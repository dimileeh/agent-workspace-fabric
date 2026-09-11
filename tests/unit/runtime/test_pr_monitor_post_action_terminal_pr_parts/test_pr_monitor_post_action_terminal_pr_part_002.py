"""#910 post-action terminal-PR guard — part 2 of 3.

The merge-blocked and merge-method-preflight notifications, the guard's fail-open
behavior (forge fault, open PR, unresolvable repo, diagnostic-event failure), the
preserved-head marker, and the seams where the agent action itself raised:
operator-hint, CI-fix, and sync-base post-agent and launch errors.

Part 1 holds the end-to-end regression and the push/pause seams; part 3 holds the
superseded-owner fencing and the cleanup-failure seam. Shared builders live in
``._helpers``; the PostgreSQL ``factory`` fixture lives in the package
``conftest``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import AgentRuntime
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    CheckFailure,
    MonitorState,
    OperatorHint,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictProtocolError
from awf.runtime.pr_monitor_runner.constants import (
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
    _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from tests.unit.runtime._merge_methods_fixtures import (
    _execute_merge,
    _MergeMethodClient,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_post_action_terminal_pr_parts._helpers import (
    _moot_events,
    _protected_block,
    _recheck_failed_events,
    _respond_to_git_probes,
    _ScriptedGh,
    _status,
)


class _TerminalAfterMergeAttemptClient(_MergeMethodClient):
    """Merge-method double whose PR goes terminal AFTER the pre-merge recheck.

    The first ``fetch_pr_status`` is the merge loop's own pre-merge recheck and
    stays open, so the loop proceeds into the escalation exactly as in production;
    every later read (i.e. the notification-boundary guard) sees the terminal PR.
    """

    def __init__(self, *, terminal: PRStatus, **kwargs: object) -> None:
        """Wrap the shared double with the post-recheck terminal snapshot."""
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._terminal = terminal

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
        retry: bool = True,
    ) -> PRStatus:
        """Report the PR as terminal on every read after the pre-merge recheck."""
        if self.fetch_pr_status_calls:
            self.fetch_pr_status_calls += 1
            return self._terminal
        return await super().fetch_pr_status(
            repo=repo,
            pr_number=pr_number,
            base_behind_count=base_behind_count,
            retry=retry,
        )


@pytest.mark.unit
async def test_merge_blocked_notification_skipped_when_pr_went_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A merge rejected because the PR merged out-of-band gets no human ping."""
    gh = _TerminalAfterMergeAttemptClient(
        terminal=_status(merged=True),
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request is not mergeable.",
            ),
        ],
    )

    terminal, _state, _sleep, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.comments == []
    moot = await _moot_events(factory, workspace_id)
    assert [event.payload["context"] for event in moot] == [  # type: ignore[index]
        "merge_blocked_notification"
    ]


@pytest.mark.unit
async def test_merge_method_preflight_notification_skipped_when_pr_went_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The merge-method preflight escalation re-reads PR state before notifying."""
    gh = _TerminalAfterMergeAttemptClient(
        terminal=_status(merged=True),
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        repo_error=GitHubClientError(
            operation="gh api repos",
            returncode=1,
            stderr="HTTP 404: Not Found",
        ),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            ),
        ],
    )

    terminal, _state, _sleep, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.comments == []
    moot = await _moot_events(factory, workspace_id)
    assert [event.payload["context"] for event in moot] == [  # type: ignore[index]
        "merge_method_preflight_notification"
    ]


# ---------------------------------------------------------------------------
# 6/7 — fail-open behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_recheck_forge_error_falls_back_to_the_existing_pause(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient forge fault on the re-fetch must not mask the original outcome."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="preserved-head")
    gh = _ScriptedGh(ForgeClientError("forge unavailable"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    with structlog.testing.capture_logs() as captured:
        result = await runner._pause_monitor_for_protected_scope_block(
            workspace_id=workspace_id,
            pr_number=42,
            pr_head_sha="abc1234567890def",
            protected_scope_block=_protected_block(),
            worktree_path=worktree,
            state=MonitorState(),
            remote_branch=f"awf/{workspace_id}",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
        )

    assert result.paused_into_blocked is True
    assert result.reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    assert any(
        entry.get("event") == "monitor.post_action_pr_terminal_recheck_failed"
        and entry.get("reason_code") == _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON
        for entry in captured
    )
    assert await _moot_events(factory, workspace_id) == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "blocked"


@pytest.mark.unit
async def test_open_pr_recheck_returns_none_and_records_nothing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status())
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    observation = await runner._post_action_pr_terminal_state(
        workspace_id=workspace_id,
        pr_number=42,
        operation_id="op",
        operation_type="comment_repair",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        context="unit_test",
    )

    assert observation is None
    assert await _moot_events(factory, workspace_id) == []
    assert await _recheck_failed_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_terminal_recheck_head_probe_is_bounded_and_best_effort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed audit-only HEAD read cannot block confirmed terminal handling."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    observed_timeouts: list[float | None] = []

    async def _failing_head_probe(
        probe_path: Path,
        *,
        timeout_seconds: float | None = None,
    ) -> str | None:
        assert probe_path == worktree
        observed_timeouts.append(timeout_seconds)
        raise OSError("cannot spawn git")

    monkeypatch.setattr(runner, "_rev_parse_head", _failing_head_probe)

    with structlog.testing.capture_logs() as captured:
        observation = await runner._post_action_pr_terminal_state(
            workspace_id=workspace_id,
            pr_number=42,
            operation_id="op-head-probe",
            operation_type="comment_repair",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            context="unit_test",
            worktree_path=worktree,
        )

    assert observation is not None
    assert observation.merged is True
    assert observation.local_head_sha is None
    assert len(observed_timeouts) == 1
    assert observed_timeouts[0] is not None
    assert observed_timeouts[0] > 0
    events = await _moot_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].payload["local_head_sha"] is None  # type: ignore[attr-defined,index]
    assert any(
        entry.get("event") == "monitor.post_action_pr_terminal_head_probe_failed"
        for entry in captured
    )


@pytest.mark.unit
async def test_recheck_forge_error_records_a_diagnostic_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The fail-open blip reaches durable workspace history, not just the log."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    gh = _ScriptedGh(ForgeClientError("forge unavailable for ghp_0123456789abcdef"))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    observation = await runner._post_action_pr_terminal_state(
        workspace_id=workspace_id,
        pr_number=42,
        operation_id="op-7",
        operation_type="comment_repair",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        context="unit_test",
    )

    assert observation is None
    # Fail-open is NOT a moot action: the caller still owns its original outcome.
    assert await _moot_events(factory, workspace_id) == []
    events = await _recheck_failed_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].reason_code == _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON  # type: ignore[attr-defined]
    payload = events[0].payload  # type: ignore[attr-defined]
    assert payload["context"] == "unit_test"
    assert payload["operation_id"] == "op-7"
    assert payload["operation_type"] == "comment_repair"
    assert payload["pr_number"] == 42
    assert "ghp_0123456789abcdef" not in payload["stderr"]


@pytest.mark.unit
async def test_recheck_diagnostic_event_failure_still_fails_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB fault writing the diagnostic must not mask the caller's outcome."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    gh = _ScriptedGh(ForgeClientError("forge unavailable"))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _boom(**_kwargs: object) -> None:
        raise SQLAlchemyError("event sink down")

    monkeypatch.setattr(runner, "_append_workspace_events", _boom)

    with structlog.testing.capture_logs() as captured:
        observation = await runner._post_action_pr_terminal_state(
            workspace_id=workspace_id,
            pr_number=42,
            operation_id="op-8",
            operation_type="comment_repair",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            context="unit_test",
        )

    assert observation is None
    assert any(
        entry.get("event") == "monitor.post_action_pr_terminal_recheck_event_failed"
        and entry.get("reason_code") == _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON
        for entry in captured
    )


@pytest.mark.unit
async def test_recheck_without_resolvable_repo_fails_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An unusable ``repo_url`` on the row leaves today's behaviour untouched."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.repo_url = "not-a-repo-url"
        await session.commit()
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    gh = _ScriptedGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    with structlog.testing.capture_logs() as captured:
        observation = await runner._post_action_pr_terminal_state(
            workspace_id=workspace_id,
            pr_number=42,
            operation_id="op",
            operation_type="comment_repair",
            context="unit_test",
        )

    assert observation is None
    assert gh.fetches == []
    assert any(
        entry.get("event") == "monitor.post_action_pr_terminal_recheck_unavailable"
        for entry in captured
    )


@pytest.mark.unit
async def test_preserved_head_marker_is_untouched_when_action_is_moot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The moot pause must not rewrite the preserved-head monitor-state marker."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="new-head")
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "original-preserved")

    await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        protected_scope_block=_protected_block(),
        worktree_path=worktree,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
    )

    assert state.threads_addressed_ids[_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY] == (
        "original-preserved"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(
            lambda: AgentVerdictProtocolError(reason_code="AGENT_VERDICT_PROTOCOL_VIOLATION"),
            id="verdict_protocol",
        ),
        pytest.param(
            lambda: ProtectedScopeDiffError("diff unavailable"), id="protected_scope_diff"
        ),
        pytest.param(
            lambda: _MonitorPolicyBlockedError("monitor policy blocked"), id="policy_blocked"
        ),
        pytest.param(
            lambda: _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
            id="runtime_ownership",
        ),
        pytest.param(
            lambda: _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING", "head object missing"),
            id="head_object_missing",
        ),
        pytest.param(
            lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"), id="mirror_hooks"
        ),
    ],
)
async def test_operator_hint_agent_error_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[], Exception],
) -> None:
    """A CLI ERROR on a merged PR must go moot, not fail or park needs_human.

    The terminal-verdict guard only covers results the CLI *returns*. Every error it
    RAISES also ends the resume — terminally failing the workspace (protocol
    violation, ownership/mirror repair failure) or arming a human notification — so
    the same re-check has to run before any of those results is built.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the seam must return before any push/pause/diff work")

    async def _raise(**_kwargs: object) -> object:
        raise make_error()

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _raise)
    monkeypatch.setattr(runner, "_protected_scope_diff_unavailable_push_result", _never)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)
    monkeypatch.setattr(runner, "_validated_git_push_result", _never)

    hint = OperatorHint(reason="operator remonitor", directive="redo the fix")
    state = MonitorState()
    state.pending_operator_hint = hint

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
        _operation_id="op_hint",
        _operation_type="operator_hint_repair",
    )

    assert result.failed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.merged is True
    # No stale human notification armed and no terminal failure recorded.
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status != "needs_human"
    assert gh.posts == []
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_operator_hint_mirror_hooks_error_still_parks_needs_human_when_pr_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not change CLI-error handling while the PR is still open."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status())  # post-action guard: PR still open
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise(**_kwargs: object) -> object:
        raise _MonitorMirrorHooksPathRepairFailedError("hooks poisoned")

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _raise)

    hint = OperatorHint(reason="operator remonitor", directive="redo the fix")
    state = MonitorState()
    state.pending_operator_hint = hint

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
        _operation_id="op_hint",
        _operation_type="operator_hint_repair",
    )

    assert result.failed is True
    assert result.reason_code == _MIRROR_HOOKS_PATH_POISONED_REASON
    assert result.stderr == "hooks poisoned"
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert len(await _moot_events(factory, workspace_id)) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(
            lambda: _MonitorPolicyBlockedError("monitor policy blocked"), id="policy_blocked"
        ),
        pytest.param(
            lambda: _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
            id="runtime_ownership",
        ),
        pytest.param(
            lambda: _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING", "head object missing"),
            id="head_object_missing",
        ),
        pytest.param(
            lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"), id="mirror_hooks"
        ),
        pytest.param(
            lambda: ProtectedScopeDiffError("diff unavailable"), id="protected_scope_diff"
        ),
    ],
)
async def test_ci_fix_post_agent_error_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[], Exception],
) -> None:
    """A post-agent CI-repair failure on a merged PR must go moot, not fail.

    The CI-repair seam re-reads PR state only after the commit sink, so every
    failure the sink RAISES used to return a ``failed`` result that bypassed the
    guard — and the loop terminally failed a workspace whose PR merged while the
    repair ran, instead of completing it as moot.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    adapter = FakeAdapter()
    adapter.queue(stdout="ci fixed")
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise(**_kwargs: object) -> bool:
        raise make_error()

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the recheck must run before any push/pause/diff work")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _raise)
    monkeypatch.setattr(runner, "_protected_scope_diff_unavailable_push_result", _never)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        operation_id="op_ci",
        operation_type="ci_repair",
    )

    assert result.failed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.merged is True
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_ci_fix_post_agent_error_still_fails_when_pr_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recheck must not change post-agent failure handling on an open PR."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    adapter = FakeAdapter()
    adapter.queue(stdout="ci fixed")
    gh = _ScriptedGh(_status())  # post-agent recheck: PR still open
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise(**_kwargs: object) -> bool:
        raise _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING", "head object missing")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _raise)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        operation_id="op_ci",
        operation_type="ci_repair",
    )

    assert result.failed is True
    assert result.reason_code == "HEAD_OBJECT_MISSING"
    assert result.pr_terminal is None
    assert len(await _moot_events(factory, workspace_id)) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(
            lambda: _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
            id="runtime_ownership",
        ),
        pytest.param(
            lambda: _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING", "head object missing"),
            id="head_object_missing",
        ),
        pytest.param(
            lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"), id="mirror_hooks"
        ),
    ],
)
async def test_ci_fix_agent_launch_error_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[], Exception],
) -> None:
    """The agent-launch handlers need the same recheck as the commit-sink ones.

    These returns sit in the FIRST ``try`` block — the agent run itself raised, so
    the commit sink never runs and the post-sink handlers pinned above are never
    reached. Without the recheck here a CI repair whose PR merged during the agent
    run still handed the loop a ``failed`` result.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise(**_kwargs: object) -> None:
        raise make_error()

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the recheck must run before any sink/push/pause work")

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _raise)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _never)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        operation_id="op_ci",
        operation_type="ci_repair",
    )

    assert result.failed is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.merged is True
    assert len(await _moot_events(factory, workspace_id)) == 1


def _respond_to_sync_base_conflict(cmd: FakeCommandRunner) -> None:
    """Drive ``_run_sync_base`` into its conflict-resolution agent branch."""
    cmd.respond_when(
        lambda args: "merge" in args and "--no-edit" in args,
        returncode=1,
        stderr="CONFLICT (content): Merge conflict in src/conflict.py",
    )
    cmd.respond_when(lambda args: "--porcelain" in args, stdout="UU src/conflict.py\n")
    _respond_to_git_probes(cmd)


@pytest.mark.unit
async def test_sync_base_post_agent_error_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-agent conflict-resolution failure on a closed PR must go moot.

    Same bypass as the CI-repair path: the sync-base guard sits after the commit
    sink, so a failure the sink raises used to reach the loop as ``failed``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_sync_base_conflict(cmd)
    adapter = FakeAdapter()
    adapter.queue(stdout="conflicts resolved")
    gh = _ScriptedGh(_status(closed=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise(**_kwargs: object) -> bool:
        raise _MonitorPolicyBlockedError("monitor policy blocked")

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the recheck must run before any push/pause work")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _raise)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)
    monkeypatch.setattr(runner, "_validated_git_push_result", _never)

    result = await runner._run_sync_base(
        workspace_id=workspace_id,
        state=MonitorState(),
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id="op_sync",
        operation_type="sync_base",
    )

    assert result.failed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.closed is True
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_sync_base_post_agent_error_still_fails_when_pr_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recheck must not change sync-base failure handling on an open PR."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_sync_base_conflict(cmd)
    adapter = FakeAdapter()
    adapter.queue(stdout="conflicts resolved")
    gh = _ScriptedGh(_status())  # post-agent recheck: PR still open
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise(**_kwargs: object) -> bool:
        raise _MonitorPolicyBlockedError("monitor policy blocked")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _raise)

    result = await runner._run_sync_base(
        workspace_id=workspace_id,
        state=MonitorState(),
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id="op_sync",
        operation_type="sync_base",
    )

    assert result.failed is True
    assert result.reason_code == "MONITOR_POLICY_BLOCKED"
    assert result.pr_terminal is None
    assert len(await _moot_events(factory, workspace_id)) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(
            lambda: AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(returncode=1, stdout="", stderr="base ref mismatch"),
                reason_code="HOSTED_GIT_PREPARATION_BASE_REF_MISMATCH",
            ),
            id="base_ref_mismatch",
        ),
        pytest.param(
            lambda: _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
            id="runtime_ownership",
        ),
        pytest.param(
            lambda: _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING", "head object missing"),
            id="head_object_missing",
        ),
        pytest.param(
            lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"), id="mirror_hooks"
        ),
    ],
)
async def test_sync_base_agent_launch_error_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[], Exception],
) -> None:
    """Sync-base returns straight out of the conflict agent launch need it too.

    The base-ref-mismatch and runtime-repair returns fire before the conflict
    commit sink, so they bypassed the post-sink guard the same way the CI-repair
    launch handlers did.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_sync_base_conflict(cmd)
    gh = _ScriptedGh(_status(closed=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise(**_kwargs: object) -> None:
        raise make_error()

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the recheck must run before any sink/push/pause work")

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _raise)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _never)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)
    monkeypatch.setattr(runner, "_validated_git_push_result", _never)

    result = await runner._run_sync_base(
        workspace_id=workspace_id,
        state=MonitorState(),
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id="op_sync",
        operation_type="sync_base",
    )

    assert result.failed is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.closed is True
    assert len(await _moot_events(factory, workspace_id)) == 1


def _provider_agent_run_error() -> AgentRunError:
    """Build the provider failure the recovery handler classifies."""
    return AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(returncode=1, stdout="", stderr="provider unavailable"),
        reason_code="PROVIDER_ERROR",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "recovery_error",
    [
        pytest.param(ProviderRecoveryFallbackError, id="fallback"),
        pytest.param(ProviderRecoveryAuthError, id="auth_failed"),
    ],
)
async def test_ci_fix_provider_recovery_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_error: type[Exception],
) -> None:
    """Provider recovery must not fail a workspace whose PR already merged.

    On the committed arm the provider handler RAISES fallback/auth out of
    ``_run_ci_fix`` entirely, so it never reaches the seam's terminal guard and
    ``runner.run()`` terminally fails the workspace. Re-read PR state first.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise_agent_error(**_kwargs: object) -> None:
        raise _provider_agent_run_error()

    async def _committed(**_kwargs: object) -> bool:
        return True

    async def _recover(*_args: object, **_kwargs: object) -> object:
        raise recovery_error()

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the recheck must run before any push/pause work")

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _raise_agent_error)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _committed)
    monkeypatch.setattr(runner, "_handle_provider_agent_run_error", _recover)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        operation_id="op_ci",
        operation_type="ci_repair",
    )

    assert result.failed is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.merged is True
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_ci_fix_provider_recovery_still_raises_when_pr_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recheck must leave provider recovery intact while the PR is open."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status())  # post-agent recheck: PR still open
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise_agent_error(**_kwargs: object) -> None:
        raise _provider_agent_run_error()

    async def _committed(**_kwargs: object) -> bool:
        return True

    async def _recover(*_args: object, **_kwargs: object) -> object:
        raise ProviderRecoveryFallbackError()

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _raise_agent_error)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _committed)
    monkeypatch.setattr(runner, "_handle_provider_agent_run_error", _recover)

    with pytest.raises(ProviderRecoveryFallbackError):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),),
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
            operation_id="op_ci",
            operation_type="ci_repair",
        )

    assert len(await _moot_events(factory, workspace_id)) == 0


@pytest.mark.unit
async def test_sync_base_provider_recovery_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync-base conflict agent needs the CI-repair path's provider recheck."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_sync_base_conflict(cmd)
    gh = _ScriptedGh(_status(closed=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise_agent_error(**_kwargs: object) -> None:
        raise _provider_agent_run_error()

    async def _committed(**_kwargs: object) -> bool:
        return True

    async def _recover(*_args: object, **_kwargs: object) -> object:
        raise ProviderRecoveryFallbackError()

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the recheck must run before any push/pause work")

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _raise_agent_error)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _committed)
    monkeypatch.setattr(runner, "_handle_provider_agent_run_error", _recover)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)
    monkeypatch.setattr(runner, "_validated_git_push_result", _never)

    result = await runner._run_sync_base(
        workspace_id=workspace_id,
        state=MonitorState(),
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id="op_sync",
        operation_type="sync_base",
    )

    assert result.failed is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.closed is True
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_sync_base_provider_recovery_still_raises_when_pr_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open PR keeps the sync-base provider fallback semantics unchanged."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_sync_base_conflict(cmd)
    gh = _ScriptedGh(_status())  # post-agent recheck: PR still open
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise_agent_error(**_kwargs: object) -> None:
        raise _provider_agent_run_error()

    async def _committed(**_kwargs: object) -> bool:
        return True

    async def _recover(*_args: object, **_kwargs: object) -> object:
        raise ProviderRecoveryFallbackError()

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _raise_agent_error)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _committed)
    monkeypatch.setattr(runner, "_handle_provider_agent_run_error", _recover)

    with pytest.raises(ProviderRecoveryFallbackError):
        await runner._run_sync_base(
            workspace_id=workspace_id,
            state=MonitorState(),
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            pr_head_sha="abc1234567890def",
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            operation_id="op_sync",
            operation_type="sync_base",
        )

    assert len(await _moot_events(factory, workspace_id)) == 0


async def _run_comment_repair_with_provider_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    recovery_error: type[Exception],
    item: str,
    gh: _ScriptedGh,
) -> tuple[str, object]:
    """Drive one fix-cycle item whose verdict helper raises provider recovery."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise_recovery(**_kwargs: object) -> object:
        raise recovery_error()

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the recheck must run before any push/pause work")

    monkeypatch.setattr(runner, "_address_thread", _raise_recovery)
    monkeypatch.setattr(runner, "_address_review_comment_result", _raise_recovery)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)
    monkeypatch.setattr(runner, "_validated_git_push_result", _never)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(
            (ReviewThread(thread_id="T1", path="src/foo.py", line=1, body_excerpt="x", author="r"),)
            if item == "thread"
            else ()
        ),
        initial_reviews=(
            () if item == "thread" else (ReviewComment(comment_id="C1", body_excerpt="x"),)
        ),
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id="op_fix",
        operation_type="comment_repair",
    )
    return workspace_id, result


@pytest.mark.unit
@pytest.mark.parametrize("item", ["thread", "review_comment"])
@pytest.mark.parametrize(
    "recovery_error",
    [
        pytest.param(ProviderRecoveryFallbackError, id="fallback"),
        pytest.param(ProviderRecoveryAuthError, id="auth_failed"),
    ],
)
async def test_comment_repair_provider_recovery_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_error: type[Exception],
    item: str,
) -> None:
    """A comment repair whose provider recovery aborts must not fail a merged PR.

    ``_handle_provider_agent_run_error`` raises fallback/auth out of the per-item
    verdict helper, so the cycle never reaches its post-loop terminal guard and
    ``runner.run()`` terminally fails the workspace. Re-read PR state first —
    the CI-repair and sync-base provider paths already do (PRRT_kwDOSJAM6s6fvT6u).
    """
    workspace_id, result = await _run_comment_repair_with_provider_recovery(
        factory,
        tmp_path,
        monkeypatch,
        recovery_error=recovery_error,
        item=item,
        gh=_ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000")),
    )

    assert result.failed is False  # type: ignore[attr-defined]
    assert result.pushed is False  # type: ignore[attr-defined]
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON  # type: ignore[attr-defined]
    assert result.pr_terminal is not None  # type: ignore[attr-defined]
    assert result.pr_terminal.merged is True  # type: ignore[attr-defined]
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
@pytest.mark.parametrize("item", ["thread", "review_comment"])
async def test_comment_repair_provider_recovery_still_raises_when_pr_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    item: str,
) -> None:
    """An open PR keeps the comment-repair provider fallback semantics unchanged."""
    with pytest.raises(ProviderRecoveryFallbackError):
        await _run_comment_repair_with_provider_recovery(
            factory,
            tmp_path,
            monkeypatch,
            recovery_error=ProviderRecoveryFallbackError,
            item=item,
            gh=_ScriptedGh(_status()),  # post-item recheck: PR still open
        )
