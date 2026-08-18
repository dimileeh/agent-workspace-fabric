"""Fail-closed FIXED evidence gate regressions (part 002)."""

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
    # Tip evidence retained until push/resolve succeed (PRRT_kwDOSJAM6s6ZnvBN).
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged


@pytest.mark.unit
async def test_salvage_retained_when_cancelled_during_post_commit_head_evaluate(
    tmp_path: Path,
) -> None:
    """Cancel mid post-commit HEAD evaluate must still retain durable salvage.

    After ``_commit_dirty_worktree`` returns with HEAD already advanced, the
    success path awaits ``_evaluate_local_head_advance`` (rev-parse / ancestry /
    tree probes) before retain+persist. An unshielded cancel there escapes with
    no item-bound evidence, so a restart's no-change FIXED becomes
    ``fixed_without_head_advance`` (PRRT_kwDOSJAM6s6Zoxg2). Capture and selective
    persist must use the same cancellation-safe pattern as sink/agent cancel.
    """
    import asyncio

    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_kwDOSJAM6s6Zoxg2"
    body_hash = "feedback_body_hash_post_commit_evaluate_cancel"
    workspace_id = "ws_post_commit_evaluate_cancel"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_post_commit_evaluate_cancel"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0
    entered_evaluate = asyncio.Event()

    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed before post-commit evaluate cancel",
        dirty=True,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
        returncode=1,
    )
    runner._worktrees_root = tmp_path

    async def _commit_dirty_worktree_returns(**_kwargs: object) -> bool:
        return True

    rev_parse_calls = 0

    async def _rev_parse_head_hangs_once(_worktree_path: Path) -> str | None:
        # Hang only the unshielded success-path probe; shielded salvage retain
        # must re-evaluate and observe the advanced HEAD.
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            entered_evaluate.set()
            await asyncio.Event().wait()
        return salvaged

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

    runner._commit_dirty_worktree = _commit_dirty_worktree_returns
    runner._rev_parse_head = _rev_parse_head_hangs_once
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
    await entered_evaluate.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert persist_state_calls == 0
    assert rev_parse_calls >= 2, "shielded retain must re-probe HEAD after cancel"
    assert persisted_snapshots, "post-commit evaluate cancel must durable-write salvage"
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
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after post-commit evaluate cancel",
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
    assert retry.reason == "confirmed salvaged fix after post-commit evaluate cancel"
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged


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
    # Tip evidence retained until push/resolve succeed (PRRT_kwDOSJAM6s6ZnvBN).
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged


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
    # Tip evidence retained until push/resolve succeed (PRRT_kwDOSJAM6s6ZnvBN).
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged


@pytest.mark.unit
async def test_cancel_during_salvage_retain_on_provider_recovery_path_propagates(
    tmp_path: Path,
) -> None:
    """Cancel mid-salvage retain on a non-cancel exit must re-raise CancelledError.

    ``_retain_failed_run_salvage_despite_cancellation`` shields retain+persist, but
    returning normally after a mid-retain cancel lets
    ``ProviderRecoveryRetryError`` callers re-raise the original error and drop
    cancellation (PRRT_kwDOSJAM6s6ZoX2e). Clarification cleanup / successful FIXED
    tip persist save+re-raise; this path must match while still durable-writing
    salvage keys.
    """
    import asyncio

    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_kwDOSJAM6s6ZoX2e"
    body_hash = "feedback_body_hash_cancel_during_recovery_salvage"
    workspace_id = "ws_cancel_during_recovery_salvage"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_cancel_during_recovery_salvage"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0
    entered_persist = asyncio.Event()
    release_persist = asyncio.Event()

    runner = _evidence_runner(
        stdout="",
        dirty=False,
        heads=[salvaged],
        head_descends=True,
        commit_trees_differ=True,
    )
    runner._worktrees_root = tmp_path

    async def _service_recovery_after_self_commit(**_kwargs: object) -> AgentRunResult:
        raise ProviderRecoveryRetryError("provider recovery retry after self-commit")

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

    async def _commit_dirty_must_not_run(**_kwargs: object) -> bool:
        raise AssertionError("recovery guard exit must not reach dirty-commit sink")

    runner._run_monitor_agent_with_service_recovery = _service_recovery_after_self_commit
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
    await entered_persist.wait()
    task.cancel()
    release_persist.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert persist_state_calls == 0
    assert persisted_snapshots, "salvage persist must complete under mid-retain cancel"
    assert earlier_unconfirmed not in persisted_snapshots[-1]
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start


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
    # Tip evidence retained until push/resolve succeed (PRRT_kwDOSJAM6s6ZnvBN).
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged


@pytest.mark.unit
async def test_unexpected_exception_salvage_persist_shielded_from_cancellation(
    tmp_path: Path,
) -> None:
    """Cancel mid-persist on unexpected Exception must still durable-write salvage.

    Successful-tip and salvage-clear paths shield ``_persist_failed_run_salvage_durably``.
    The unexpected agent Exception path retained keys then used a bare await; cancel
    during that write drops item-bound salvage while HEAD already advanced, so a
    later no-change FIXED becomes ``fixed_without_head_advance``
    (PRRT_kwDOSJAM6s6ZovMQ).
    """
    import asyncio

    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_kwDOSJAM6s6ZovMQ_unexpected"
    body_hash = "feedback_body_hash_unexpected_persist_shield"
    workspace_id = "ws_unexpected_persist_shield"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_unexpected_persist_shield"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0
    entered_persist = asyncio.Event()
    release_persist = asyncio.Event()

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

    async def _commit_dirty_clean(**_kwargs: object) -> bool:
        return False

    runner._deps = SimpleNamespace(adapter=SimpleNamespace(run=_agent_self_commits_then_crashes))
    runner._persist_failed_run_salvage_durably = _persist_failed_run_salvage_durably
    runner._persist_state = _persist_state
    runner._commit_dirty_worktree = _commit_dirty_clean

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
    assert persisted_snapshots, "unexpected-Exception salvage persist must complete under cancel"
    assert earlier_unconfirmed not in persisted_snapshots[-1]
    assert persisted_snapshots[-1].get(_salvaged_fix_head_state_key(item_id)) == salvaged
    assert persisted_snapshots[-1].get(_salvaged_fix_body_hash_state_key(item_id)) == body_hash
    assert persisted_snapshots[-1].get(_salvaged_fix_start_state_key(item_id)) == start

    reloaded = MonitorState()
    reloaded.threads_addressed_ids.update(persisted_snapshots[-1])
    assert earlier_unconfirmed not in reloaded.threads_addressed_ids

    retry_runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: confirmed tip after unexpected mid-persist cancel",
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
    assert retry.reason == "confirmed tip after unexpected mid-persist cancel"
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged


@pytest.mark.unit
async def test_unexpected_exception_head_evaluate_shielded_from_cancellation(
    tmp_path: Path,
) -> None:
    """Cancel mid HEAD probe on unexpected Exception must still retain salvage.

    After self-commit (or dirty sink), the unexpected ``except Exception`` path
    probes HEAD then retain+persists. An unshielded await lets CancelledError
    escape from inside that handler — sibling ``except CancelledError`` cannot
    catch it — so restart has the tip but no item-bound salvage and a no-change
    FIXED becomes ``fixed_without_head_advance`` (PRRT_kwDOSJAM6s6Zo8Dt). Probe,
    retain, and selective persist must use the cancellation-safe helper.
    """
    import asyncio

    from awf.runtime.monitor_state_keys import (
        _salvaged_fix_body_hash_state_key,
        _salvaged_fix_head_state_key,
        _salvaged_fix_start_state_key,
    )

    start = "a" * 40
    salvaged = "b" * 40
    item_id = "PRRT_kwDOSJAM6s6Zo8Dt"
    body_hash = "feedback_body_hash_unexpected_evaluate_cancel"
    workspace_id = "ws_unexpected_evaluate_cancel"
    earlier_unconfirmed = "PRRT_earlier_unconfirmed_unexpected_evaluate_cancel"
    (tmp_path / workspace_id).mkdir()
    state = MonitorState()
    state.mark_addressed(earlier_unconfirmed, "fix_committed")
    persisted_snapshots: list[dict[str, str]] = []
    persist_state_calls = 0
    entered_evaluate = asyncio.Event()
    release_evaluate = asyncio.Event()

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

    rev_parse_calls = 0

    async def _rev_parse_head_hangs_on_unexpected_probe(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        entered_evaluate.set()
        await release_evaluate.wait()
        return salvaged

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
    runner._rev_parse_head = _rev_parse_head_hangs_on_unexpected_probe
    runner._persist_failed_run_salvage_durably = _persist_failed_run_salvage_durably
    runner._persist_state = _persist_state
    runner._commit_dirty_worktree = _commit_dirty_clean

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
    await entered_evaluate.wait()
    task.cancel()
    release_evaluate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert persist_state_calls == 0
    assert rev_parse_calls >= 1
    assert persisted_snapshots, "unexpected-Exception HEAD probe cancel must durable-write salvage"
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
        stdout="AWF-VERDICT: FIXED: confirmed salvaged fix after unexpected evaluate cancel",
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
    assert retry.reason == "confirmed salvaged fix after unexpected evaluate cancel"
    assert reloaded.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == salvaged
