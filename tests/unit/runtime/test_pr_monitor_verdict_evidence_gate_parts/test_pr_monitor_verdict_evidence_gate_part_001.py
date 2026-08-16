"""Fail-closed FIXED evidence gate regressions (part 001)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    MonitorState,
)
from awf.runtime.pr_monitor_runner import comments
from tests.postgres import postgres_test_engine
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
async def test_fixed_claim_dirty_commit_with_unverified_head_stays_unresolved(
    tmp_path: Path,
) -> None:
    """Present worktree + failed post-commit HEAD probe must fail closed.

    After a multi-item burst reset to the remote tip, a dirty sink commit can
    still succeed while a transient ``_rev_parse_head`` failure leaves ancestry
    unevaluable. Accepting ``committed_dirty_changes`` as FIXED in that case
    would let a replacement tip push while dropping an earlier item's fix.
    """
    start = "b" * 40
    workspace_id = "ws_fixed_dirty_head_probe_fail"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: reset tip then committed dirty edits",
        dirty=True,
        heads=[None],
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
    # Tip evidence must survive FIXED accept so a later push/resolve requeue can
    # still prove a no-change FIXED at this HEAD (PRRT_kwDOSJAM6s6ZnvBN).
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert state.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start


@pytest.mark.unit
async def test_successful_fixed_evidence_survives_publication_requeue(
    tmp_path: Path,
) -> None:
    """Ordinary FIXED tip evidence must survive push/resolve addressed clear.

    After a contentful FIXED, fix_cycle may clear the addressed verdict when push
    fails or resolve is requeued. The retry starts at the same tip and correctly
    prints FIXED with no further change — that must remain fix_committed, not
    fixed_without_head_advance (PRRT_kwDOSJAM6s6ZnvBN).
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )
    from awf.runtime.pr_monitor_runner.helpers import _clear_addressed_state_by_id

    start = "a" * 40
    tip = "b" * 40
    item_id = "PRRT_kwDOSJAM6s6ZnvBN"
    body_hash = "feedback_body_hash_publication"
    workspace_id = "ws_publication_evidence"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()

    first_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: ordinary successful commit",
        dirty=True,
        heads=[tip],
        head_descends=True,
        commit_trees_differ=True,
    )
    first_runner._worktrees_root = tmp_path

    first = await comments._invoke_cli_for_verdict_result(
        first_runner,
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
    assert first.verdict == "fix_committed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == tip
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert state.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start

    state.mark_addressed(item_id, "fix_committed")
    _clear_addressed_state_by_id(state, item_id)
    assert item_id not in state.threads_addressed_ids
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == tip
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert state.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: still fixed after push failure",
        dirty=False,
        heads=[tip],
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
        operation_start_head=tip,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "still fixed after push failure"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == tip


@pytest.mark.unit
async def test_successful_fixed_tip_evidence_persisted_before_return(
    tmp_path: Path,
) -> None:
    """Successful FIXED tip keys must durable-persist before returning.

    Failed/cancelled paths already call ``_persist_failed_run_salvage_durably``.
    Ordinary successful FIXED previously wrote ``__salvaged_fix_*`` only into
    in-memory ``MonitorState``; full ``_persist_state`` waits until ``_execute``
    returns. Cancel during the subsequent settle sleep reloads no item-bound
    evidence while HEAD already contains the commit, so a correct no-change
    FIXED retry becomes ``fixed_without_head_advance``
    (PRRT_kwDOSJAM6s6Znz-0). Persist only salvage keys on successful accept —
    never full ``_persist_state``.
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    tip = "b" * 40
    item_id = "PRRT_kwDOSJAM6s6Znz-0"
    body_hash = "feedback_body_hash_successful_persist"
    workspace_id = "ws_successful_fixed_tip_persist"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_successful_persist"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0

    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: ordinary successful commit",
        dirty=True,
        heads=[tip],
        head_descends=True,
        commit_trees_differ=True,
    )
    runner._worktrees_root = tmp_path

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

    runner._persist_failed_run_salvage_durably = _persist_failed_run_salvage_durably
    runner._persist_state = _persist_state

    first = await comments._invoke_cli_for_verdict_result(
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
    assert first.verdict == "fix_committed"
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == tip
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert state.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start
    assert persisted_snapshots, "successful FIXED must durable-persist tip evidence"
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == tip
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start
    assert persist_state_calls == 0
    assert earlier_unconfirmed not in persisted_snapshots[-1]

    # Simulate worker reload after settle-sleep cancel: only durable salvage keys.
    reloaded = MonitorState()
    reloaded.threads_addressed_ids.update(persisted_snapshots[-1])
    assert earlier_unconfirmed not in reloaded.threads_addressed_ids

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed tip after settle cancel reload",
        dirty=False,
        heads=[tip],
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
        operation_start_head=tip,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed tip after settle cancel reload"
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == tip


@pytest.mark.unit
async def test_successful_fixed_tip_persist_shielded_from_cancellation(
    tmp_path: Path,
) -> None:
    """Cancel mid-persist on successful FIXED must still durable-write tip keys.

    Failed/cancelled paths shield retain+persist via
    ``_retain_failed_run_salvage_despite_cancellation``. The successful FIXED
    selective write was a bare ``await``; cancel during that DB operation drops
    tip evidence while HEAD already advanced, so a later no-change FIXED
    becomes ``fixed_without_head_advance`` (PRRT_kwDOSJAM6s6ZoXc9).
    """
    import asyncio

    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    tip = "b" * 40
    item_id = "PRRT_kwDOSJAM6s6ZoXc9"
    body_hash = "feedback_body_hash_successful_persist_shield"
    workspace_id = "ws_successful_fixed_tip_persist_shield"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_successful_persist_shield"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0
    entered_persist = asyncio.Event()
    release_persist = asyncio.Event()

    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: ordinary successful commit",
        dirty=True,
        heads=[tip],
        head_descends=True,
        commit_trees_differ=True,
    )
    runner._worktrees_root = tmp_path

    async def _persist_failed_run_salvage_durably(
        _workspace_id: str,
        persisted: MonitorState,
        *,
        salvage_item_id: str,
    ) -> None:
        entered_persist.set()
        await release_persist.wait()
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
    await entered_persist.wait()
    task.cancel()
    release_persist.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert persist_state_calls == 0
    assert persisted_snapshots, "successful FIXED tip persist must complete under cancel"
    assert earlier_unconfirmed not in persisted_snapshots[-1]
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == tip
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start

    reloaded = MonitorState()
    reloaded.threads_addressed_ids.update(persisted_snapshots[-1])
    assert earlier_unconfirmed not in reloaded.threads_addressed_ids

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed tip after mid-persist cancel reload",
        dirty=False,
        heads=[tip],
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
        operation_start_head=tip,
        evidence_item_id=item_id,
        evidence_body_hash=body_hash,
    )
    assert retry.verdict == "fix_committed"
    assert retry.reason == "confirmed tip after mid-persist cancel reload"
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == tip


@pytest.mark.unit
@pytest.mark.parametrize(
    ("synthetic_stdout", "synthetic_reason"),
    [
        (
            "Committed a fix without a marker",
            "unrecognized_or_markerless_verdict",
        ),
        (
            "AWF-VERDICT: FIXD: garbled token",
            "garbled_verdict_marker",
        ),
    ],
)
async def test_salvage_preserved_across_successful_synthetic_needs_human(
    tmp_path: Path,
    synthetic_stdout: str,
    synthetic_reason: str,
) -> None:
    """Successful fail-closed parse must not drop retained salvage.

    A prior failed run may have committed valid salvage. A later successful but
    markerless/garbled invocation returns synthetic needs_human — clearing
    salvage there strands a subsequent no-change FIXED as
    fixed_without_head_advance (PRRT_kwDOSJAM6s6ZncGe). Clear only for explicit
    NEEDS_HUMAN, matching ``_should_clear_salvage_for_parsed_verdict``.
    """
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = f"PRRT_salvage_synthetic_{synthetic_reason}"
    body_hash = f"feedback_body_hash_synthetic_{synthetic_reason}"
    workspace_id = f"ws_salvage_synthetic_{synthetic_reason}"
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

    synthetic_runner = _evidence_runner(
        stdout=synthetic_stdout,
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    synthetic_runner._worktrees_root = tmp_path

    synthetic = await comments._invoke_cli_for_verdict_result(
        synthetic_runner,
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
    assert synthetic.verdict == "needs_human"
    assert synthetic.reason == synthetic_reason
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert state.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == start

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after guidance",
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
    assert retry.reason == "confirmed salvaged fix after guidance"
    # Tip evidence retained until push/resolve succeed (PRRT_kwDOSJAM6s6ZnvBN).
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged


@pytest.mark.unit
async def test_successful_explicit_needs_human_clears_retained_salvage(
    tmp_path: Path,
) -> None:
    """Explicit NEEDS_HUMAN on a successful run must drop retained salvage."""
    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_salvage_explicit_needs_human"
    body_hash = "feedback_body_hash_explicit_needs_human"
    workspace_id = "ws_salvage_explicit_needs_human"
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

    explicit_runner = _evidence_runner(
        stdout="AWF-VERDICT: NEEDS_HUMAN: maintainer must choose after salvage",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    explicit_runner._worktrees_root = tmp_path

    explicit = await comments._invoke_cli_for_verdict_result(
        explicit_runner,
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
    assert explicit.verdict == "needs_human"
    assert explicit.reason == "maintainer must choose after salvage"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_start_state_key(item_id) not in state.threads_addressed_ids

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: should not reuse cleared salvage",
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
