"""Fail-closed FIXED evidence gate and per-item isolation regressions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
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
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_task_tag_threading import (
    _MonitorAgentServiceRecoveryRunner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _evidence_runner(
    *,
    stdout: str,
    dirty: bool,
    heads: list[str | None] | None = None,
    returncode: int = 0,
    head_descends: bool | None = None,
    commit_trees_differ: bool | None = None,
    commit_changes_present: bool | None = None,
    provider_recovery_raise: BaseException | None = None,
    commit_dirty_raises: BaseException | None = None,
) -> SimpleNamespace:
    """Stub runner for ``_invoke_cli_for_verdict_result`` evidence checks."""
    head_iter = iter(heads or [])

    async def _suppress(_workspace_id: str) -> bool:
        return False

    async def _adapter_run(**_kwargs: object) -> AgentRunResult:
        if returncode != 0:
            from awf.adapters.base import AgentRunError
            from awf.db.enums import AgentRuntime

            raise AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(returncode=returncode, stdout=stdout, stderr="fail"),
                reason_code="AGENT_CLI_FAILED",
            )
        return AgentRunResult(returncode=0, stdout=stdout, stderr="")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        if commit_dirty_raises is not None:
            raise commit_dirty_raises
        return dirty

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        try:
            return next(head_iter)
        except StopIteration:
            return heads[-1] if heads else None

    async def _head_descends_from(
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path
        if head_descends is not None:
            return head_descends
        return ancestor.lower() != descendant.lower()

    async def _commit_trees_differ(
        *,
        worktree_path: Path,
        left: str,
        right: str,
    ) -> bool:
        del worktree_path
        if commit_trees_differ is not None:
            return commit_trees_differ
        # Default: distinct SHAs imply a contentful advance for legacy stubs.
        return left.lower() != right.lower()

    async def _commit_changes_present_in_head(
        *,
        worktree_path: Path,
        commit: str,
        head: str,
        baseline: str | None = None,
    ) -> bool:
        del worktree_path, commit, head, baseline
        if commit_changes_present is not None:
            return commit_changes_present
        # Default: descendant tips preserve salvage content for legacy stubs.
        return True

    async def _handle_provider_agent_run_error(
        _workspace_id: str,
        _exc: object,
        *,
        state: object | None = None,
    ) -> None:
        del state
        if provider_recovery_raise is not None:
            raise provider_recovery_raise

    return _MonitorAgentServiceRecoveryRunner(
        _worktrees_root=Path("/tmp"),
        _provider_recovery_suppresses_cli=_suppress,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _rev_parse_head=_rev_parse_head,
        _head_descends_from=_head_descends_from,
        _commit_trees_differ=_commit_trees_differ,
        _commit_changes_present_in_head=_commit_changes_present_in_head,
        _handle_provider_agent_run_error=_handle_provider_agent_run_error,
        _deps=SimpleNamespace(adapter=SimpleNamespace(run=_adapter_run)),
    )


@pytest.mark.unit
async def test_fixed_claim_with_dirty_commit_is_fix_committed() -> None:
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: renamed helper",
        dirty=True,
        heads=["b" * 40],
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_fixed_dirty",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head="a" * 40,
    )

    assert result.verdict == "fix_committed"
    assert result.reason == "renamed helper"


@pytest.mark.unit
async def test_explicit_fixed_without_head_advance_stays_unresolved() -> None:
    start = "a" * 40
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed but no commit",
        dirty=False,
        heads=[start],
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_fixed_no_change",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_operator_hint_fixed_without_head_advance_is_accepted() -> None:
    """Operator hints may complete GitHub-side work without a commit."""
    start = "a" * 40
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: replied on GitHub only",
        dirty=False,
        heads=[start],
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_operator_hint_no_code",
        prompt="p",
        commit_message="fix: address operator hint",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
        require_fix_evidence=False,
    )

    assert result.verdict == "fix_committed"
    assert result.reason == "replied on GitHub only"


@pytest.mark.unit
async def test_operator_hint_fixed_without_evidence_still_agent_failed_on_cli_error() -> None:
    start = "a" * 40
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=False,
        heads=[start],
        returncode=1,
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_operator_hint_cli_fail",
        prompt="p",
        commit_message="fix: address operator hint",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
        require_fix_evidence=False,
    )

    assert result.verdict == "agent_failed"


@pytest.mark.unit
async def test_fixed_claim_with_forward_head_advance_is_fix_committed(
    tmp_path: Path,
) -> None:
    start = "a" * 40
    end = "b" * 40
    workspace_id = "ws_fixed_forward"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: agent committed locally",
        dirty=False,
        heads=[end],
        head_descends=True,
        commit_trees_differ=True,
    )
    runner._worktrees_root = tmp_path

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "fix_committed"
    assert result.reason == "agent committed locally"


@pytest.mark.unit
async def test_fixed_claim_with_empty_forward_commit_stays_unresolved(
    tmp_path: Path,
) -> None:
    """Forward ancestry alone must not accept FIXED when the commit tree is unchanged."""
    start = "a" * 40
    empty_descendant = "b" * 40
    workspace_id = "ws_fixed_empty_forward"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: empty allow-empty commit",
        dirty=False,
        heads=[empty_descendant],
        head_descends=True,
        commit_trees_differ=False,
    )
    runner._worktrees_root = tmp_path

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_hosted_fixed_with_empty_forward_commit_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Hosted empty-commit tip must not satisfy FIXED via forward ancestry alone."""
    start = "a" * 40
    empty_synced = "b" * 40
    workspace_id = "ws_hosted_empty_forward"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: hosted empty tip",
        dirty=False,
        heads=[start],
        head_descends=True,
        commit_trees_differ=False,
    )
    runner._worktrees_root = tmp_path
    state = MonitorState(last_push_sha=start)

    async def _run_with_empty_hosted_advance(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        state_arg.last_push_sha = empty_synced
        state_arg.hosted_terminal_head_advanced = True
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted empty tip",
            stderr="",
        )

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _run_with_empty_hosted_advance
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_fixed_claim_with_backward_head_move_stays_unresolved(
    tmp_path: Path,
) -> None:
    start = "b" * 40
    older = "a" * 40
    workspace_id = "ws_fixed_backward"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: reset to older tip",
        dirty=False,
        heads=[older],
        head_descends=False,
    )
    runner._worktrees_root = tmp_path

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_fixed_claim_backward_head_with_dirty_commit_stays_unresolved(
    tmp_path: Path,
) -> None:
    """Known non-descendant HEAD must not be accepted via committed_dirty_changes."""
    start = "b" * 40
    older = "a" * 40
    workspace_id = "ws_fixed_backward_dirty"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: reset then left dirty edits",
        dirty=True,
        heads=[older],
        head_descends=False,
    )
    runner._worktrees_root = tmp_path

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_markerless_output_never_upgraded_by_dirty_commit() -> None:
    runner = _evidence_runner(
        stdout="Committed a fix without a marker",
        dirty=True,
        heads=["b" * 40],
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_markerless_dirty",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head="a" * 40,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "unrecognized_or_markerless_verdict"


@pytest.mark.unit
async def test_fixed_with_dirty_commit_still_agent_failed_on_cli_nonzero() -> None:
    """Dirty salvage after a nonzero CLI exit must not resolve FIXED."""
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed after crash with dirty edits",
        dirty=True,
        heads=["b" * 40],
        head_descends=True,
        returncode=1,
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_dirty_nonzero_fixed",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head="a" * 40,
    )

    assert result.verdict == "agent_failed"


@pytest.mark.unit
async def test_salvaged_dirty_fix_evidence_carries_into_successful_retry(
    tmp_path: Path,
) -> None:
    """Failed salvage must not resolve; successful FIXED retry may confirm it."""
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_retry"
    body_hash = "feedback_body_hash_v1"
    workspace_id = "ws_salvage_retry"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert state.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed salvaged fix"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_start_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_salvage_retained_before_provider_recovery_retry_raise(
    tmp_path: Path,
) -> None:
    """Dirty salvage must persist even when provider recovery raises retry.

    ``_handle_provider_agent_run_error`` persists then raises for retry/fallback.
    Recording salvage only after that call loses evidence for a later FIXED that
    starts at the salvage tip and would otherwise become fixed_without_head_advance.
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_before_recovery_raise"
    body_hash = "feedback_body_hash_recovery_raise"
    workspace_id = "ws_salvage_before_recovery_raise"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during provider outage",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
        provider_recovery_raise=ProviderRecoveryRetryError(),
    )
    failed_runner._worktrees_root = tmp_path

    with pytest.raises(ProviderRecoveryRetryError):
        await comments._invoke_cli_for_verdict_result(
            failed_runner,
            workspace_id=workspace_id,
            prompt="p",
            commit_message="fix: x",
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=start,
            evidence_item_id=item_id,
            evidence_body_hash=body_hash,
        )
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after recovery",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed salvaged fix after recovery"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    "sink_kind",
    [
        "post_commit_ownership_repair",
        "protected_scope_provider_recovery",
        "service_recovery_failed",
        "service_recovery_superseded",
        "cancelled_after_commit",
    ],
)
async def test_salvage_retained_when_commit_sink_raises_after_head_advance(
    tmp_path: Path,
    sink_kind: str,
) -> None:
    """Dirty salvage must persist when ``_commit_dirty_worktree`` raises after HEAD advances.

    Post-commit ownership repair failure and protected-scope repair self-commit
    followed by provider recovery both advance HEAD inside the sink, then raise
    before the success-path salvage block. ``CancelledError`` is a BaseException,
    so cancellation during post-commit ownership repair has the same gap unless
    salvage is retained before propagating (PRRT_kwDOSJAM6s6Zmn1b). Without
    retaining evidence from that exception path, a later no-change FIXED at the
    tip becomes ``fixed_without_head_advance`` (PRRT_kwDOSJAM6s6ZmirT). All
    commit-sink raises — including service-recovery failed/superseded, which the
    outer runner catches without ``_persist_state`` — must selectively durable-
    persist salvage before re-raise so a worker reload does not drop the keys
    (PRRT_kwDOSJAM6s6ZmsZQ, PRRT_kwDOSJAM6s6Zm-Yt).
    """
    import asyncio

    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )
    from awf.runtime.pr_monitor_runner.types import (
        ProviderRecoveryRetryError,
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorAgentServiceRecoveryFailedError,
        _MonitorAgentServiceRecoverySupersededError,
    )

    sink_exc: BaseException
    if sink_kind == "post_commit_ownership_repair":
        sink_exc = _MonitorAgentRuntimeOwnershipRepairFailedError(
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
        )
    elif sink_kind == "cancelled_after_commit":
        sink_exc = asyncio.CancelledError()
    elif sink_kind == "service_recovery_failed":
        sink_exc = _MonitorAgentServiceRecoveryFailedError(
            "service recovery failed after sink HEAD advance"
        )
    elif sink_kind == "service_recovery_superseded":
        sink_exc = _MonitorAgentServiceRecoverySupersededError(
            "service recovery superseded after sink HEAD advance"
        )
    else:
        sink_exc = ProviderRecoveryRetryError()

    start = "a" * 40
    salvaged = "b" * 40
    item_id = f"PRRT_salvage_sink_raise_{sink_kind}"
    body_hash = f"feedback_body_hash_sink_raise_{sink_kind}"
    workspace_id = f"ws_salvage_sink_raise_{sink_kind}"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    persisted_snapshots: list[dict[str, str]] = []

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed before sink raised",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
        commit_dirty_raises=sink_exc,
    )
    failed_runner._worktrees_root = tmp_path

    async def _persist_failed_run_salvage_durably(
        _workspace_id: str,
        persisted: MonitorState,
        *,
        salvage_item_id: str,
    ) -> None:
        # Simulate merge-only durable write: only the three salvage keys for this item.
        snap: dict[str, str] = {}
        for key in (
            _salvaged_fix_head_state_key(salvage_item_id),
            _salvaged_fix_body_hash_state_key(salvage_item_id),
            _salvaged_fix_start_state_key(salvage_item_id),
        ):
            value = persisted.threads_addressed_ids.get(key)
            if value is not None:
                snap[key] = value
        persisted_snapshots.append(snap)

    async def _persist_state(_workspace_id: str, _persisted: MonitorState) -> None:
        raise AssertionError("commit-sink salvage must not call full _persist_state")

    failed_runner._persist_failed_run_salvage_durably = _persist_failed_run_salvage_durably
    failed_runner._persist_state = _persist_state

    with pytest.raises(type(sink_exc)):
        await comments._invoke_cli_for_verdict_result(
            failed_runner,
            workspace_id=workspace_id,
            prompt="p",
            commit_message="fix: x",
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=start,
            evidence_item_id=item_id,
            evidence_body_hash=body_hash,
        )
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert state.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start
    # Every commit-sink raise must flush salvage before re-raise. Exception paths
    # cannot rely on the outer runner: recovery failed/superseded return without
    # ``_persist_state`` (PRRT_kwDOSJAM6s6Zm-Yt).
    assert persisted_snapshots
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start
    # Simulate worker reload: drop in-memory keys and restore from the durable
    # snapshot that the commit-sink path persisted.
    state = MonitorState()
    state.threads_addressed_ids.update(persisted_snapshots[-1])

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after sink raise",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed salvaged fix after sink raise"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_salvage_retained_when_commit_sink_task_cancelled_after_head_advance(
    tmp_path: Path,
) -> None:
    """Task cancel mid-sink after commit must still retain item-bound salvage.

    On Python 3.11+, catching ``CancelledError`` leaves cancellation pending, so
    a naive post-sink ``await`` would re-raise before salvage is recorded. Shield
    HEAD capture + retention + selective salvage persist, then propagate
    cancellation (PRRT_kwDOSJAM6s6Zmn1b, PRRT_kwDOSJAM6s6ZmsZQ). Assert durability
    via a simulated DB reload — in-memory-only keys would vanish after restart.
    Full ``_persist_state`` must not run: mid-burst unconfirmed verdicts must not
    flush (PRRT_kwDOSJAM6s6Zmur3).
    """
    import asyncio

    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_sink_task_cancel"
    body_hash = "feedback_body_hash_sink_task_cancel"
    workspace_id = "ws_salvage_sink_task_cancel"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_fix"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    # Mid-burst earlier item marked addressed but not yet pushed/resolved.
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0

    entered_sink = asyncio.Event()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed before sink cancelled",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    runner._worktrees_root = tmp_path

    async def _commit_dirty_worktree_then_hang(**_kwargs: object) -> bool:
        # Commit already advanced HEAD (rev-parse returns ``salvaged``); hang in
        # post-commit ownership repair until the worker cancels the task.
        entered_sink.set()
        await asyncio.Event().wait()
        return True

    async def _persist_failed_run_salvage_durably(
        _workspace_id: str,
        persisted: MonitorState,
        *,
        salvage_item_id: str,
    ) -> None:
        snap: dict[str, str] = {}
        for key in (
            _salvaged_fix_head_state_key(salvage_item_id),
            _salvaged_fix_body_hash_state_key(salvage_item_id),
            _salvaged_fix_start_state_key(salvage_item_id),
        ):
            value = persisted.threads_addressed_ids.get(key)
            if value is not None:
                snap[key] = value
        persisted_snapshots.append(snap)

    async def _persist_state(_workspace_id: str, _persisted: MonitorState) -> None:
        nonlocal persist_state_calls
        persist_state_calls += 1

    runner._commit_dirty_worktree = _commit_dirty_worktree_then_hang
    runner._persist_failed_run_salvage_durably = _persist_failed_run_salvage_durably
    runner._persist_state = _persist_state

    task = asyncio.create_task(
        comments._invoke_cli_for_verdict_result(
            runner,
            workspace_id=workspace_id,
            prompt="p",
            commit_message="fix: x",
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=start,
            evidence_item_id=item_id,
            evidence_body_hash=body_hash,
        )
    )
    await entered_sink.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert persist_state_calls == 0
    assert persisted_snapshots
    assert earlier_unconfirmed not in persisted_snapshots[-1]
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start
    # In-memory unconfirmed marker remains; only salvage keys were durably written.
    assert state.threads_addressed_ids.get(earlier_unconfirmed) == "fix_committed"

    # Worker restart: discard the live object and reload from the durable snapshot.
    reloaded = MonitorState()
    reloaded.threads_addressed_ids.update(persisted_snapshots[-1])
    assert earlier_unconfirmed not in reloaded.threads_addressed_ids
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert (
        reloaded.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    )
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after sink cancel",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path
    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=reloaded,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed salvaged fix after sink cancel"
    assert _salvaged_fix_head_state_key(item_id) not in reloaded.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in reloaded.threads_addressed_ids


@pytest.mark.unit
async def test_salvage_retained_when_agent_cancelled_after_self_commit(
    tmp_path: Path,
) -> None:
    """Cancel mid-agent after self-commit must retain salvage before re-raise.

    ``CancelledError`` bypasses ``except Exception`` around the agent invocation.
    Unlike cancellation after ``_commit_dirty_worktree`` creates a commit, a
    restart here previously had no persisted salvage keys: the next invocation
    starts at the advanced HEAD, and a no-change FIXED is rejected as
    ``fixed_without_head_advance`` (PRRT_kwDOSJAM6s6ZmviO). Catch cancellation
    around the agent run, retain/persist any HEAD advance, then re-raise.
    """
    import asyncio

    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_agent_cancel"
    body_hash = "feedback_body_hash_agent_cancel"
    workspace_id = "ws_salvage_agent_cancel"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_agent_cancel"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0

    entered_agent = asyncio.Event()
    runner = _evidence_runner(
        stdout="",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    runner._worktrees_root = tmp_path

    async def _agent_self_commits_then_hangs(**_kwargs: object) -> AgentRunResult:
        # Agent already advanced HEAD (self-commit); hang until worker cancels.
        entered_agent.set()
        await asyncio.Event().wait()
        return AgentRunResult(returncode=0, stdout="AWF-VERDICT: FIXED: never reached", stderr="")

    async def _persist_failed_run_salvage_durably(
        _workspace_id: str,
        persisted: MonitorState,
        *,
        salvage_item_id: str,
    ) -> None:
        snap: dict[str, str] = {}
        for key in (
            _salvaged_fix_head_state_key(salvage_item_id),
            _salvaged_fix_body_hash_state_key(salvage_item_id),
            _salvaged_fix_start_state_key(salvage_item_id),
        ):
            value = persisted.threads_addressed_ids.get(key)
            if value is not None:
                snap[key] = value
        persisted_snapshots.append(snap)

    async def _persist_state(_workspace_id: str, _persisted: MonitorState) -> None:
        nonlocal persist_state_calls
        persist_state_calls += 1

    async def _commit_dirty_must_not_run(**_kwargs: object) -> bool:
        raise AssertionError("cancel during agent must not reach dirty-commit sink")

    runner._deps = SimpleNamespace(adapter=SimpleNamespace(run=_agent_self_commits_then_hangs))
    runner._persist_failed_run_salvage_durably = _persist_failed_run_salvage_durably
    runner._persist_state = _persist_state
    runner._commit_dirty_worktree = _commit_dirty_must_not_run

    task = asyncio.create_task(
        comments._invoke_cli_for_verdict_result(
            runner,
            workspace_id=workspace_id,
            prompt="p",
            commit_message="fix: x",
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=start,
            evidence_item_id=item_id,
            evidence_body_hash=body_hash,
        )
    )
    await entered_agent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert persist_state_calls == 0
    assert persisted_snapshots
    assert earlier_unconfirmed not in persisted_snapshots[-1]
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start
    assert state.threads_addressed_ids.get(earlier_unconfirmed) == "fix_committed"

    reloaded = MonitorState()
    reloaded.threads_addressed_ids.update(persisted_snapshots[-1])
    assert earlier_unconfirmed not in reloaded.threads_addressed_ids
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert (
        reloaded.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    )
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after agent cancel",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=reloaded,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed salvaged fix after agent cancel"
    assert _salvaged_fix_head_state_key(item_id) not in reloaded.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in reloaded.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    "guard_exc_kind",
    [
        "provider_recovery_retry",
        "service_recovery_superseded",
        "ownership_repair_failed",
    ],
)
async def test_salvage_retained_when_service_recovery_guard_exits_after_self_commit(
    tmp_path: Path,
    guard_exc_kind: str,
) -> None:
    """Service-recovery guard exits after self-commit must retain salvage.

    When the agent self-commits then times out, service recovery can restart and
    subsequently raise ``ProviderRecoveryRetryError``,
    ``_MonitorAgentServiceRecoverySupersededError``, or an ownership error from
    pre-launch guards. Those used bare re-raise paths so only cancellation
    reached salvage capture; the next monitor starts at the committed HEAD
    without persisted evidence and a no-change FIXED becomes
    ``fixed_without_head_advance`` (PRRT_kwDOSJAM6s6Zm0PB). Capture and persist
    any HEAD advance before propagating these exits.
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )
    from awf.runtime.pr_monitor_runner.types import (
        ProviderRecoveryRetryError,
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorAgentServiceRecoverySupersededError,
    )

    guard_exc: BaseException
    if guard_exc_kind == "provider_recovery_retry":
        guard_exc = ProviderRecoveryRetryError("provider recovery retry after self-commit")
    elif guard_exc_kind == "service_recovery_superseded":
        guard_exc = _MonitorAgentServiceRecoverySupersededError(
            "service recovery superseded after self-commit"
        )
    else:
        guard_exc = _MonitorAgentRuntimeOwnershipRepairFailedError(
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
        )
    start = "a" * 40
    salvaged = "b" * 40
    item_id = f"PRRT_salvage_recovery_guard_{guard_exc_kind}"
    body_hash = f"feedback_body_hash_recovery_guard_{guard_exc_kind}"
    workspace_id = f"ws_salvage_recovery_guard_{guard_exc_kind}"
    earlier_unconfirmed = f"PRRT_earlier_unconfirmed_recovery_guard_{guard_exc_kind}"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0

    runner = _evidence_runner(
        stdout="",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    runner._worktrees_root = tmp_path

    async def _service_recovery_after_self_commit(**_kwargs: object) -> AgentRunResult:
        # Agent already advanced HEAD; recovery/pre-launch guard then exits.
        raise guard_exc

    async def _persist_failed_run_salvage_durably(
        _workspace_id: str,
        persisted: MonitorState,
        *,
        salvage_item_id: str,
    ) -> None:
        snap: dict[str, str] = {}
        for key in (
            _salvaged_fix_head_state_key(salvage_item_id),
            _salvaged_fix_body_hash_state_key(salvage_item_id),
            _salvaged_fix_start_state_key(salvage_item_id),
        ):
            value = persisted.threads_addressed_ids.get(key)
            if value is not None:
                snap[key] = value
        persisted_snapshots.append(snap)

    async def _persist_state(_workspace_id: str, _persisted: MonitorState) -> None:
        nonlocal persist_state_calls
        persist_state_calls += 1

    async def _commit_dirty_must_not_run(**_kwargs: object) -> bool:
        raise AssertionError("recovery guard exit must not reach dirty-commit sink")

    runner._run_monitor_agent_with_service_recovery = _service_recovery_after_self_commit
    runner._persist_failed_run_salvage_durably = _persist_failed_run_salvage_durably
    runner._persist_state = _persist_state
    runner._commit_dirty_worktree = _commit_dirty_must_not_run

    with pytest.raises(type(guard_exc)):
        await comments._invoke_cli_for_verdict_result(
            runner,
            workspace_id=workspace_id,
            prompt="p",
            commit_message="fix: x",
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=start,
            evidence_item_id=item_id,
            evidence_body_hash=body_hash,
        )

    assert persist_state_calls == 0
    assert persisted_snapshots
    assert earlier_unconfirmed not in persisted_snapshots[-1]
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start
    assert state.threads_addressed_ids.get(earlier_unconfirmed) == "fix_committed"

    reloaded = MonitorState()
    reloaded.threads_addressed_ids.update(persisted_snapshots[-1])
    assert earlier_unconfirmed not in reloaded.threads_addressed_ids
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert (
        reloaded.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    )
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after recovery guard exit",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=reloaded,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed salvaged fix after recovery guard exit"
    assert _salvaged_fix_head_state_key(item_id) not in reloaded.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in reloaded.threads_addressed_ids


@pytest.mark.unit
async def test_salvage_persisted_when_agent_raises_unexpected_after_self_commit(
    tmp_path: Path,
) -> None:
    """Unexpected agent Exception after self-commit must durable-persist salvage.

    The unexpected ``except Exception`` path retains ``__salvaged_fix_*`` keys in
    memory then re-raises. Unlike ``CancelledError``, it previously skipped
    ``_persist_failed_run_salvage_durably``. A monitor-cycle end or worker reload
    drops those keys while the salvage commit remains, so a later no-change FIXED
    becomes ``fixed_without_head_advance`` (PRRT_kwDOSJAM6s6Zmzxr). Persist only
    salvage keys — never full ``_persist_state`` — before re-raising.
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_agent_unexpected"
    body_hash = "feedback_body_hash_agent_unexpected"
    workspace_id = "ws_salvage_agent_unexpected"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_agent_unexpected"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0

    runner = _evidence_runner(
        stdout="",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    runner._worktrees_root = tmp_path

    async def _agent_self_commits_then_crashes(**_kwargs: object) -> AgentRunResult:
        raise RuntimeError("adapter crashed after self-commit")

    async def _persist_failed_run_salvage_durably(
        _workspace_id: str,
        persisted: MonitorState,
        *,
        salvage_item_id: str,
    ) -> None:
        snap: dict[str, str] = {}
        for key in (
            _salvaged_fix_head_state_key(salvage_item_id),
            _salvaged_fix_body_hash_state_key(salvage_item_id),
            _salvaged_fix_start_state_key(salvage_item_id),
        ):
            value = persisted.threads_addressed_ids.get(key)
            if value is not None:
                snap[key] = value
        persisted_snapshots.append(snap)

    async def _persist_state(_workspace_id: str, _persisted: MonitorState) -> None:
        nonlocal persist_state_calls
        persist_state_calls += 1

    async def _commit_dirty_clean(**_kwargs: object) -> bool:
        return False

    runner._deps = SimpleNamespace(adapter=SimpleNamespace(run=_agent_self_commits_then_crashes))
    runner._persist_failed_run_salvage_durably = _persist_failed_run_salvage_durably
    runner._persist_state = _persist_state
    # Agent already advanced HEAD; worktree is clean — dirty sink is a no-op.
    runner._commit_dirty_worktree = _commit_dirty_clean

    with pytest.raises(RuntimeError, match="adapter crashed after self-commit"):
        await comments._invoke_cli_for_verdict_result(
            runner,
            workspace_id=workspace_id,
            prompt="p",
            commit_message="fix: x",
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=start,
            evidence_item_id=item_id,
            evidence_body_hash=body_hash,
        )

    assert persist_state_calls == 0
    assert persisted_snapshots
    assert earlier_unconfirmed not in persisted_snapshots[-1]
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start
    assert state.threads_addressed_ids.get(earlier_unconfirmed) == "fix_committed"

    reloaded = MonitorState()
    reloaded.threads_addressed_ids.update(persisted_snapshots[-1])
    assert earlier_unconfirmed not in reloaded.threads_addressed_ids
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert (
        reloaded.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    )
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after unexpected crash",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=reloaded,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed salvaged fix after unexpected crash"
    assert _salvaged_fix_head_state_key(item_id) not in reloaded.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in reloaded.threads_addressed_ids


@pytest.mark.unit
async def test_persist_failed_run_salvage_durably_merges_only_salvage_keys(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Cancel salvage persist must merge only ``__salvaged_fix_*`` keys onto DB.

    Mid-burst unconfirmed ``fix_committed`` markers in memory must not flush
    (PRRT_kwDOSJAM6s6Zmur3 / #305).
    """
    from awf.db.repositories import WorkspaceRepository
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    item_id = "PRRT_cancel_salvage_only"
    head_key = _salvaged_fix_head_state_key(item_id)
    body_key = _salvaged_fix_body_hash_state_key(item_id)
    start_key = _salvaged_fix_start_state_key(item_id)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            "already_confirmed": "false_positive",
            head_key: "stale_head",
        }
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    state = MonitorState(
        threads_addressed_ids={
            "PRRT_unconfirmed": "fix_committed",
            head_key: "b" * 40,
            body_key: "body_hash",
            start_key: "a" * 40,
        }
    )
    await runner._persist_failed_run_salvage_durably(
        workspace_id,
        state,
        salvage_item_id=item_id,
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        persisted = ws.monitor_threads_addressed or {}
        assert persisted.get("already_confirmed") == "false_positive"
        assert "PRRT_unconfirmed" not in persisted
        assert persisted.get(head_key) == "b" * 40
        assert persisted.get(body_key) == "body_hash"
        assert persisted.get(start_key) == "a" * 40

    # Already-equal values: no-op write (coverage for unchanged merge branch).
    await runner._persist_failed_run_salvage_durably(
        workspace_id,
        state,
        salvage_item_id=item_id,
    )


@pytest.mark.unit
async def test_persist_failed_run_salvage_durably_clears_absent_keys_and_noops(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Clear path removes stale salvage keys; missing workspace / no-op are safe."""
    from awf.db.repositories import WorkspaceRepository
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    item_id = "PRRT_cancel_salvage_clear"
    head_key = _salvaged_fix_head_state_key(item_id)
    body_key = _salvaged_fix_body_hash_state_key(item_id)
    start_key = _salvaged_fix_start_state_key(item_id)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            head_key: "old_head",
            body_key: "old_body",
            start_key: "old_start",
            "keep_me": "defer",
        }
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # In-memory salvage cleared (e.g. explicit false_positive during cancel retain).
    state = MonitorState(threads_addressed_ids={"unrelated": "needs_human"})
    await runner._persist_failed_run_salvage_durably(
        workspace_id,
        state,
        salvage_item_id=item_id,
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        persisted = ws.monitor_threads_addressed or {}
        assert head_key not in persisted
        assert body_key not in persisted
        assert start_key not in persisted
        assert persisted.get("keep_me") == "defer"
        assert "unrelated" not in persisted

    # Missing workspace: no-op.
    await runner._persist_failed_run_salvage_durably(
        "ws_does_not_exist",
        state,
        salvage_item_id=item_id,
    )
    # Already-absent keys: second clear is a no-op write.
    await runner._persist_failed_run_salvage_durably(
        workspace_id,
        state,
        salvage_item_id=item_id,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "failed_verdict"),
    [
        (
            "AWF-VERDICT: FALSE POSITIVE: printed before crash with dirty edits",
            "agent_failed",
        ),
        (
            "AWF-VERDICT: DEFER: printed before crash with dirty edits",
            "agent_failed",
        ),
        (
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose after dirty crash",
            "needs_human",
        ),
    ],
)
async def test_non_fixed_crash_clears_dirty_salvage_before_retry(
    tmp_path: Path,
    stdout: str,
    failed_verdict: str,
) -> None:
    """Explicit non-FIXED crash must not leave salvage for a later FIXED retry."""
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_non_fixed_crash"
    body_hash = "feedback_body_hash_non_fixed"
    workspace_id = "ws_salvage_non_fixed_crash"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout=stdout,
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == failed_verdict
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: should not reuse non-fixed salvage",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        "AWF-VERDICT: FALSE POSITIVE: printed before provider recovery raise",
        "AWF-VERDICT: DEFER: printed before provider recovery raise",
        "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose before recovery raise",
    ],
)
async def test_non_fixed_salvage_cleared_before_provider_recovery_raise(
    tmp_path: Path,
    stdout: str,
) -> None:
    """Explicit non-FIXED salvage must clear even when recovery raises.

    Salvage is recorded before ``_handle_provider_agent_run_error``. When that
    call raises retry/fallback, later parse cleanup never runs — so reject
    explicit non-FIXED salvage before the raising recovery handler.
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_non_fixed_recovery_raise"
    body_hash = "feedback_body_hash_non_fixed_recovery"
    workspace_id = "ws_salvage_non_fixed_recovery_raise"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout=stdout,
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
        provider_recovery_raise=ProviderRecoveryRetryError(),
    )
    failed_runner._worktrees_root = tmp_path

    with pytest.raises(ProviderRecoveryRetryError):
        await comments._invoke_cli_for_verdict_result(
            failed_runner,
            workspace_id=workspace_id,
            prompt="p",
            commit_message="fix: x",
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=start,
            evidence_item_id=item_id,
            evidence_body_hash=body_hash,
        )
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: should not reuse non-fixed salvage after recovery",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_salvaged_fix_evidence_rejects_feedback_body_change(
    tmp_path: Path,
) -> None:
    """Retained salvage must not confirm FIXED after the feedback body changes."""
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_body_change"
    workspace_id = "ws_salvage_body_change"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash="body_hash_before_edit",
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert (
        state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id))
        == "body_hash_before_edit"
    )

    # Simulate agent_failed skipping stale-body cleanup while the reviewer edits.
    state.mark_addressed(item_id, "agent_failed")

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: reuse salvage after edited feedback",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=salvaged,
        evidence_item_id=item_id,
        evidence_body_hash="body_hash_after_edit",
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_salvaged_fix_evidence_survives_later_head_advance(
    tmp_path: Path,
) -> None:
    """Retained salvage must count when a later burst tip descends from it."""
    from awf.runtime.monitor_state_keys import _salvaged_fix_head_state_key

    start = "a" * 40
    salvaged = "b" * 40
    later = "c" * 40
    item_id = "PRRT_salvage_later_head"
    body_hash = "feedback_body_hash_later"
    workspace_id = "ws_salvage_later_head"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged

    # Another item moved HEAD past the salvage tip; this retry starts/ends there.
    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after burst",
        dirty=False,
        heads=[later],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=later,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed salvaged fix after burst"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_salvaged_fix_evidence_rejects_descendant_that_reverts_salvage(
    tmp_path: Path,
) -> None:
    """Ancestry alone must not accept a tip that undoes the salvaged change.

    Item A salvages at H1; item B creates descendant H2 that reverts H1's
    content. A no-change retry of A at H2 must not resolve FIXED, and the
    undone salvage tip must be invalidated.
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    reverted = "c" * 40
    item_id = "PRRT_salvage_reverted_descendant"
    body_hash = "feedback_body_hash_reverted"
    workspace_id = "ws_salvage_reverted_descendant"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged

    # Later tip descends from salvage but undoes its content; no-change retry.
    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: reuse reverted salvage descendant",
        dirty=False,
        heads=[reverted],
        head_descends=True,
        commit_trees_differ=True,
        commit_changes_present=False,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=reverted,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_salvaged_fix_evidence_fail_closed_without_content_helper(
    tmp_path: Path,
) -> None:
    """Descendant salvage reuse must not accept ancestry when content helper is absent."""
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    later = "c" * 40
    item_id = "PRRT_salvage_no_content_helper"
    body_hash = "feedback_body_hash_no_helper"
    workspace_id = "ws_salvage_no_content_helper"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: ancestry without content proof",
        dirty=False,
        heads=[later],
        head_descends=True,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path
    del retry_runner._commit_changes_present_in_head

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=later,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash


@pytest.mark.unit
async def test_salvaged_fix_evidence_fail_closed_without_retained_start(
    tmp_path: Path,
) -> None:
    """Descendant salvage reuse must not confirm when the start SHA is missing.

    Legacy retained tip+body without the failed-run start cannot prove the full
    multi-commit delta (PRRT_kwDOSJAM6s6ZmG-B).
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    salvaged = "b" * 40
    later = "c" * 40
    item_id = "PRRT_salvage_no_start"
    body_hash = "feedback_body_hash_no_start"
    workspace_id = "ws_salvage_no_start"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(_salvaged_fix_head_state_key(item_id), salvaged)
    state.mark_addressed(_salvaged_fix_body_hash_state_key(item_id), body_hash)

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: reuse without start baseline",
        dirty=False,
        heads=[later],
        head_descends=True,
        commit_trees_differ=True,
        commit_changes_present=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=later,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_start_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_salvaged_fix_evidence_rejects_backward_reset_to_retained_tip(
    tmp_path: Path,
) -> None:
    """Equality salvage must not accept a retry that resets HEAD behind start.

    Salvage H1 plus a later item tip H2 must not resolve when the retried agent
    checks out H1 again — that discards H2 while the start→end ancestry gate
    already rejected the backward move.
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    later = "c" * 40
    item_id = "PRRT_salvage_backward_reset"
    body_hash = "feedback_body_hash_backward"
    workspace_id = "ws_salvage_backward_reset"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged

    # Another item advanced to later; retry starts there then resets to salvage.
    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: reuse salvage after resetting past tip",
        dirty=False,
        heads=[salvaged],
        head_descends=False,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=later,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash


@pytest.mark.unit
async def test_salvaged_fix_evidence_rejects_end_not_descending_from_retry_start(
    tmp_path: Path,
) -> None:
    """Non-equal salvage reuse must keep end as a descendant of retry start."""
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    later = "c" * 40
    orphan = "d" * 40
    item_id = "PRRT_salvage_orphan_from_retained"
    body_hash = "feedback_body_hash_orphan"
    workspace_id = "ws_salvage_orphan_from_retained"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged

    # Graph: start→salvaged→later; orphan descends from salvaged but not later.
    ancestry = {
        (start.lower(), salvaged.lower()),
        (start.lower(), later.lower()),
        (start.lower(), orphan.lower()),
        (salvaged.lower(), later.lower()),
        (salvaged.lower(), orphan.lower()),
    }

    head_iter = iter([orphan])

    async def _suppress(_workspace_id: str) -> bool:
        return False

    async def _adapter_run(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout="AWF-VERDICT: FIXED: orphan tip", stderr="")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return False

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        try:
            return next(head_iter)
        except StopIteration:
            return orphan

    async def _head_descends_from(
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path
        a = ancestor.lower()
        d = descendant.lower()
        return a == d or (a, d) in ancestry

    async def _commit_trees_differ(
        *,
        worktree_path: Path,
        left: str,
        right: str,
    ) -> bool:
        del worktree_path
        return left.lower() != right.lower()

    async def _handle_provider_agent_run_error(
        _workspace_id: str,
        _exc: object,
        *,
        state: object | None = None,
    ) -> None:
        del state

    retry_runner = _MonitorAgentServiceRecoveryRunner(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_suppress,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _rev_parse_head=_rev_parse_head,
        _head_descends_from=_head_descends_from,
        _commit_trees_differ=_commit_trees_differ,
        _handle_provider_agent_run_error=_handle_provider_agent_run_error,
        _deps=SimpleNamespace(adapter=SimpleNamespace(run=_adapter_run)),
    )

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=later,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash


@pytest.mark.unit
async def test_salvaged_fix_evidence_rejects_non_descendant_head(
    tmp_path: Path,
) -> None:
    """Retained salvage must not count when HEAD left the salvage ancestry."""
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    unrelated = "d" * 40
    item_id = "PRRT_salvage_non_descendant"
    body_hash = "feedback_body_hash_non_desc"
    workspace_id = "ws_salvage_non_descendant"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed without salvage ancestry",
        dirty=False,
        heads=[unrelated],
        head_descends=False,
        commit_trees_differ=True,
    )
    retry_runner._worktrees_root = tmp_path

    retry = await comments._invoke_cli_for_verdict_result(
        retry_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=unrelated,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "needs_human"
    assert retry.reason == "fixed_without_head_advance"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash


@pytest.mark.unit
async def test_salvaged_fix_evidence_does_not_leak_to_other_item(
    tmp_path: Path,
) -> None:
    """Another item must not inherit a prior item's retained salvage head."""
    from awf.runtime.monitor_state_keys import _salvaged_fix_head_state_key

    start = "a" * 40
    salvaged = "b" * 40
    workspace_id = "ws_salvage_leak"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    failed_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    failed_runner._worktrees_root = tmp_path

    failed = await comments._invoke_cli_for_verdict_result(
        failed_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        evidence_item_id="item_one",
        evidence_body_hash="body_one",
    )
    assert failed.verdict == "agent_failed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key("item_one")) == salvaged

    other_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: unrelated item no change",
        dirty=False,
        heads=[salvaged],
    )
    other_runner._worktrees_root = tmp_path

    other = await comments._invoke_cli_for_verdict_result(
        other_runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=salvaged,
        evidence_item_id="item_two",
        evidence_body_hash="body_two",
    )
    assert other.verdict == "needs_human"
    assert other.reason == "fixed_without_head_advance"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key("item_one")) == salvaged


@pytest.mark.unit
async def test_isolated_clarification_worktree_does_not_plant_salvage_keys(
    tmp_path: Path,
) -> None:
    """Reason-only isolated checkouts must not retain __salvaged_fix_* keys.

    Clarification reasks run in a disposable worktree whose commits are discarded
    on cleanup. Binding evidence ids would plant orphan salvage SHAs that later
    primary-worktree FIXED retries could mis-attribute (PRRT_kwDOSJAM6s6ZmikP).
    """
    from awf.adapters.base import AgentRunError
    from awf.db.enums import AgentRuntime
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    isolated_tip = "b" * 40
    item_id = "PRRT_isolated_reask_salvage"
    body_hash = "feedback_body_hash_isolated_reask"
    workspace_id = "ws_isolated_reask_salvage"
    isolated = tmp_path / ".awf-needs-human-reask-test"
    isolated.mkdir()
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    stdout = "AWF-VERDICT: FIXED: claimed during isolated crash"

    async def _suppress(_workspace_id: str) -> bool:
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        raise AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(returncode=1, stdout=stdout, stderr="fail"),
            reason_code="AGENT_CLI_FAILED",
        )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return False

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return isolated_tip

    async def _head_descends_from(
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path
        return ancestor.lower() != descendant.lower()

    async def _commit_trees_differ(
        *,
        worktree_path: Path,
        left: str,
        right: str,
    ) -> bool:
        del worktree_path
        return left.lower() != right.lower()

    async def _handle_provider_agent_run_error(
        _workspace_id: str,
        _exc: object,
        *,
        state: object | None = None,
    ) -> None:
        del state

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_suppress,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _rev_parse_head=_rev_parse_head,
        _head_descends_from=_head_descends_from,
        _commit_trees_differ=_commit_trees_differ,
        _handle_provider_agent_run_error=_handle_provider_agent_run_error,
    )

    failed = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="state the reason",
        commit_message="fix: address thread",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
        commit_dirty_changes=False,
        isolated_worktree_host_path=isolated,
        isolated_worktree_ref=start,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert failed.verdict == "agent_failed"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_start_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "expected_verdict", "expected_reason"),
    [
        (
            "AWF-VERDICT: FALSE POSITIVE: reviewer misread the guard",
            "false_positive",
            "reviewer misread the guard",
        ),
        (
            "AWF-VERDICT: DEFER: track follow-up outside this PR",
            "defer",
            "track follow-up outside this PR",
        ),
        (
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose the policy",
            "needs_human",
            "maintainer must choose the policy",
        ),
    ],
)
async def test_explicit_non_fix_verdicts_survive_dirty_or_hosted_advance(
    stdout: str,
    expected_verdict: str,
    expected_reason: str,
) -> None:
    start = "a" * 40
    runner = _evidence_runner(stdout=stdout, dirty=True, heads=["b" * 40])
    state = MonitorState()

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_retain_explicit",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == expected_verdict
    assert result.reason == expected_reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        "AWF-VERDICT: FALSE POSITIVE: printed before crash",
        "AWF-VERDICT: DEFER: printed before crash",
    ],
)
async def test_explicit_false_positive_or_defer_ignored_on_cli_failure(
    stdout: str,
) -> None:
    """Nonzero CLI exit must not resolve via a pre-crash FALSE POSITIVE/DEFER."""
    start = "a" * 40
    runner = _evidence_runner(stdout=stdout, dirty=False, heads=[start], returncode=1)

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_cli_fail_explicit_non_fix",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "agent_failed"


@pytest.mark.unit
async def test_hosted_fixed_requires_terminal_head_advance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    start = "a" * 40
    synced = "b" * 40
    workspace_id = "ws_hosted_fixed"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: hosted repair committed",
        dirty=False,
        heads=[start],
        head_descends=True,
    )
    runner._worktrees_root = tmp_path
    state = MonitorState(last_push_sha=start)

    async def _run_with_hosted_advance(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        state_arg.last_push_sha = synced
        state_arg.hosted_terminal_head_advanced = True
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted repair committed",
            stderr="",
        )

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _run_with_hosted_advance
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "fix_committed"
    assert result.reason == "hosted repair committed"


@pytest.mark.unit
async def test_hosted_fixed_ignored_on_cli_failure_even_with_head_advance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failed hosted run must not resolve FIXED even after terminal SHA sync."""
    from awf.adapters.base import AgentRunError
    from awf.db.enums import AgentRuntime

    start = "a" * 40
    synced = "b" * 40
    workspace_id = "ws_hosted_cli_fail_advance"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: printed before hosted failure",
        dirty=False,
        heads=[synced],
        head_descends=True,
        returncode=1,
    )
    runner._worktrees_root = tmp_path
    state = MonitorState(last_push_sha=start)

    async def _run_fail_after_hosted_sync(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        # Mirrors _run_monitor_agent_with_service_recovery: sync terminal SHA /
        # set advance evidence, then re-raise AgentRunError.
        state_arg.last_push_sha = synced
        state_arg.hosted_terminal_head_advanced = True
        raise AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="AWF-VERDICT: FIXED: printed before hosted failure",
                stderr="hosted adapter failed",
            ),
            reason_code="AGENT_CLI_FAILED",
        )

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _run_fail_after_hosted_sync
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "agent_failed"


@pytest.mark.unit
async def test_hosted_fixed_without_head_advance_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    start = "a" * 40
    workspace_id = "ws_hosted_no_advance"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: hosted no advance",
        dirty=False,
        heads=[start],
    )
    runner._worktrees_root = tmp_path
    state = MonitorState(last_push_sha=start)

    async def _run_without_advance(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        state_arg.hosted_terminal_head_advanced = False
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted no advance",
            stderr="",
        )

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _run_without_advance)

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_hosted_lateral_rewrite_does_not_count_as_fix_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Non-forward hosted tip rewrite must not satisfy FIXED via the advance flag."""
    start = "a" * 40
    lateral = "c" * 40
    workspace_id = "ws_hosted_lateral"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: hosted force-push dropped the fix",
        dirty=False,
        heads=[lateral],
        head_descends=False,
    )
    runner._worktrees_root = tmp_path
    state = MonitorState(last_push_sha=start)

    async def _run_with_lateral_hosted_rewrite(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        # Simulate _record_hosted_terminal_head_sync wrongly trusting SHA inequality
        # alone (pre-fix), or a stale True flag after a non-descendant sync tip.
        state_arg.last_push_sha = lateral
        state_arg.hosted_terminal_head_advanced = True
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted force-push dropped the fix",
            stderr="",
        )

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _run_with_lateral_hosted_rewrite
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_hosted_advance_does_not_accept_markerless_parse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    start = "a" * 40
    synced = "b" * 40
    workspace_id = "ws_hosted_markerless"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="plain hosted reply with no marker",
        dirty=False,
        heads=[synced],
        head_descends=True,
    )
    runner._worktrees_root = tmp_path
    state = MonitorState(last_push_sha=start)

    async def _run_markerless_with_advance(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        state_arg.last_push_sha = synced
        state_arg.hosted_terminal_head_advanced = True
        return AgentRunResult(
            returncode=0,
            stdout="plain hosted reply with no marker",
            stderr="",
        )

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _run_markerless_with_advance
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "unrecognized_or_markerless_verdict"


@pytest.mark.unit
async def test_another_thread_commit_does_not_leak_into_later_item_evidence() -> None:
    """Item 2 start head is post-item-1; no-op FIXED must not inherit item-1 evidence."""
    item1_start = "a" * 40
    item1_end = "b" * 40
    item2_start = item1_end

    first = await comments._invoke_cli_for_verdict_result(
        _evidence_runner(
            stdout="AWF-VERDICT: FIXED: first thread",
            dirty=True,
            heads=[item1_end],
        ),
        workspace_id="ws_leak_1",
        prompt="p",
        commit_message="fix: 1",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=item1_start,
    )
    assert first.verdict == "fix_committed"

    second = await comments._invoke_cli_for_verdict_result(
        _evidence_runner(
            stdout="AWF-VERDICT: FIXED: second thread no change",
            dirty=False,
            heads=[item2_start],
        ),
        workspace_id="ws_leak_2",
        prompt="p",
        commit_message="fix: 2",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=item2_start,
    )
    assert second.verdict == "needs_human"
    assert second.reason == "fixed_without_head_advance"


class _ResolveCaptureClient:
    def __init__(self, runner: FakeCommandRunner) -> None:
        self._runner = runner
        self.resolved: list[str] = []

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
        retry: bool = True,
    ) -> PRStatus:
        del repo, pr_number, base_behind_count, retry
        return PRStatus(
            number=42,
            head_sha="abc1234567890def",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        )

    async def resolve_thread(self, *, thread_id: str) -> None:
        self.resolved.append(thread_id)


@pytest.mark.unit
async def test_fix_cycle_two_item_burst_only_evidenced_fixed_resolves(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First item FIXED+evidence resolves; second markerless stays unresolved."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD after push
    gh = _ResolveCaptureClient(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    async def _address_thread(**kwargs: object) -> str:
        thread = kwargs["thread"]
        assert isinstance(thread, ReviewThread)
        if thread.thread_id == "T_first":
            return "fix_committed"
        return "needs_human"

    async def _protected_scope_push_block(**_kwargs: object) -> None:
        return None

    async def _validated_git_push_result(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_scope_push_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_git_push_result)

    state = MonitorState()
    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(
            ReviewThread(
                thread_id="T_first",
                path="src/a.py",
                line=1,
                body_excerpt="fix first",
                author="reviewer",
            ),
            ReviewThread(
                thread_id="T_second",
                path="src/b.py",
                line=2,
                body_excerpt="fix second",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert state.threads_addressed_ids.get("T_first") == "fix_committed"
    assert state.threads_addressed_ids.get("T_second") == "needs_human"
    assert gh.resolved == ["T_first"]
