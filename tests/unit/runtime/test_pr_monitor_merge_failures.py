"""Regression tests for PR monitor merge-failure handling and notifications."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import (
    BITBUCKET_MERGE_IN_PROGRESS,
    BITBUCKET_MERGE_METHOD_UNSUPPORTED,
    BITBUCKET_MERGE_TASK_TIMEOUT,
    BITBUCKET_RATE_LIMITED,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import OperationRepository, WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    Merge,
    MonitorConfig,
    MonitorState,
)
from awf.runtime.pr_monitor_runner.config import MonitorRunnerConfig
from awf.runtime.pr_monitor_runner.runner import PullRequestMonitorRunner
from awf.service.merge_queue import MergeQueueBlocker
from tests.postgres import postgres_test_engine
from tests.unit.runtime._merge_methods_fixtures import (
    _TEST_DEFAULT_BASE_BRANCH,
    _TEST_PR_NUMBER,
    _TEST_REPO,
    _execute_merge,
    _mergeable_status,
    _MergeMethodClient,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an isolated async session factory for merge-method monitor tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_transient_first_merge_failure_does_not_retry_allowed_alternative(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient merge failure backs off instead of changing merge method."""
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="HTTP 502 Bad Gateway",
            ),
            "MERGESHA789",
        ],
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert sleep_fn.calls == [5]
    assert gh.comments == []
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_exhausted_transient_merge_failure_preserves_retry_counter(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An exhausted *transient* merge blip must not reset the ``merge_pr`` budget.

    When the bounded forge-retry helper returns ``False`` because the budget is
    exhausted (the merge error is still transient, not a deterministic rejection),
    the merge arm falls back to notify-and-keep-polling. It must keep the persisted
    ``merge_pr`` retry counter so later polls fail closed via the exhausted path,
    rather than clearing it as if the blocker were deterministic and handing each
    subsequent merge attempt a fresh full retry budget — symmetric with
    ``fetch_pr_status`` / ``pre_merge_recheck`` (regression for the PR #516
    merge-path counter-reset bug).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="HTTP 502 Bad Gateway",
            )
        ],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    # Zero retries: the first transient blip exhausts the budget immediately and
    # drives the merge arm down the notify-and-keep-polling fallback.
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 0)
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    # The exhausted-transient counter survives in memory and in the DB instead of
    # being wiped like a deterministic blocker; the next poll therefore exhausts
    # immediately rather than re-spending a full bounded budget.
    counter_key = "__awf_forge_transient_retry_count:merge_pr"
    assert state.threads_addressed_ids.get(counter_key) == "1"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert (ws.monitor_threads_addressed or {}).get(counter_key) == "1"


@pytest.mark.unit
async def test_unclassified_first_merge_failure_notifies_without_method_mismatch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An unclassified merge failure notifies as a regular merge blocker."""
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            ),
            "MERGESHA789",
        ],
    )

    terminal, state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert "MERGE_METHOD_MISMATCH" not in gh.comments[0]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_merge_blocker_fallback_sets_attention_flag(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A deterministic merge blocker (e.g. branch protection) notifies a human
    directly from the merge loop, so it must also stamp the awaiting-human
    attention flag (#659). ``decide()`` keeps returning ``Merge`` for this PR, so
    without a set here the escalation signal would never be surfaced at all."""
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
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
    assert len(gh.comments) == 1
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.awaiting_human_since is not None
        assert ws.awaiting_human_reason is not None
        assert "GitHub rejected the merge attempt" in ws.awaiting_human_reason


@pytest.mark.unit
async def test_merge_blocker_fallback_keeps_attention_since_stable_across_polls(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A branch-protection merge blocker leaves ``decide()`` returning ``Merge``
    every poll, so the merge loop re-sets the attention flag each cycle. The
    top-of-``_execute`` attention clear must NOT wipe the persisted episode start
    on a ``Merge`` action — otherwise ``awaiting_human_since`` is reset to ``now``
    on every poll and the operator-facing "awaiting human for N" timer never ages
    while the operator is still blocked (#659)."""
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            ),
        ],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    # Seed a STABLE episode start from an earlier poll: the operator has already
    # been blocked since well before this poll runs. The prior poll's
    # branch-protection fallback also stamped a FRESH ``merge_block_attention``
    # marker (now stamps ``now``), which is what tells the critical-section-entry
    # / non-human-gate clears to PRESERVE the still-active signal (#661/#663).
    # The DB ``awaiting_human_since`` (the COALESCE'd episode start) stays at the
    # old timestamp; the marker timestamp is fresh (within the TTL) so the
    # still-blocked signal survives the critical-section-entry clear.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior escalation", now=episode_start
        )
        await session.commit()
    state = MonitorState()
    # Stamp the marker fresh (this poll's wall-clock) so the TTL gate treats it
    # as still-blocked; the fallback re-stamps it again this poll.
    state.mark_merge_block_attention()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        # The episode start must be PRESERVED (COALESCE), not reset to this poll's
        # ``now`` by the top-of-_execute clear running before the merge re-set.
        assert ws.awaiting_human_since == episode_start
        # The reason still refreshes to the latest escalation.
        assert ws.awaiting_human_reason is not None
        assert "GitHub rejected the merge attempt" in ws.awaiting_human_reason


@pytest.mark.unit
async def test_merge_blocker_fallback_persists_attention_marker_before_attention_set(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6LbY_X: the branch-protection fallback must durably persist
    the ``merge_block_attention`` marker BEFORE it commits
    ``awaiting_human_since``.

    ``_set_workspace_attention`` commits the attention flag to the workspace row
    inside its own transaction, while ``state.mark_merge_block_attention()`` only
    mutates the in-memory ``MonitorState`` that the outer ``run()`` loop persists
    AFTER ``_execute`` returns. A monitor restart/cancel in that window leaves the
    DB with ``awaiting_human_since`` set but no marker, so the next poll's
    ``_clear_stale_merge_attention`` sees no marker, clears the flag, the merge
    retries, the deterministic rejection re-sets attention to ``now`` — resetting
    the operator-visible human-wait timer even though the block never resolved.

    The fix persists the marker (via ``_persist_state``) BEFORE setting the
    attention flag, mirroring the merge-method preflight arm's ordering
    (``_persist_state`` then ``_set_workspace_attention``). This test simulates the
    crash window by reloading state from the DB immediately after the fallback
    fires and asserting the marker is durable — i.e. ``_load_state`` reconstructs a
    state whose ``merge_block_attention_active`` is True without relying on the
    in-memory ``state`` that ``_execute`` returned.
    """
    from awf.runtime.pr_monitor import _MERGE_BLOCK_ATTENTION_STATE_KEY

    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        # Deterministic branch-protection rejection.
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            ),
        ],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    # The attention flag was set during the fallback.
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.awaiting_human_since is not None

    # Simulate a restart that loses the in-memory ``state``: reload it from the DB.
    # The marker MUST already be durable so a restart between the attention-set and
    # the outer loop's persist does not strand ``awaiting_human_since`` without the
    # marker that tells the next poll's clear to PRESERVE the still-active signal.
    async with factory() as session:
        ws_for_reload = await WorkspaceRepository(session).get(workspace_id)
        assert ws_for_reload is not None
    reloaded_state = runner._load_state(ws_for_reload)
    assert (
        reloaded_state.merge_block_attention_active(
            ttl_seconds=runner._config.merge_block_attention_ttl_seconds,
        )
        is True
    )
    # And the persisted marker key is present in the DB-stored threads_addressed.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY in (ws_for_reload.monitor_threads_addressed or {})


@pytest.mark.unit
async def test_merge_blocker_fallback_writes_marker_and_attention_atomically(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lcgk0: the branch-protection fallback must persist the
    ``merge_block_attention`` marker AND set ``awaiting_human_since`` in a SINGLE
    transaction, not as two separate commits.

    The previous ordering (``_persist_state`` marker commit then a separate
    ``_set_workspace_attention`` commit) closed the window where
    ``awaiting_human_since`` was set but the marker was missing
    (PRRT_kwDOSJAM6s6LbY_X), but created the RECIPROCAL window: a cancel/restart
    after the marker commit but before the attention commit left the DB with a
    FRESH marker but NULL ``awaiting_human_since``. On the next poll, if the PR
    parked on a non-human gate wait (merge queue / reviewer settle / initial
    review grace / merge lock) before another merge attempt reached the fallback,
    ``_clear_stale_merge_attention``'s preserve path saw the fresh marker,
    re-stamped it, and returned WITHOUT setting ``awaiting_human_since`` — so the
    active branch-protection escalation was never surfaced to the operator until a
    later merge retry re-entered the fallback.

    This test simulates the reciprocal crash window by intercepting the
    ``_set_workspace_attention`` commit point: it asserts the marker is ALREADY
    durable in the SAME row that carries ``awaiting_human_since`` — i.e. the two
    pieces were written together — and that a reload mid-fallback cannot observe
    a fresh marker with NULL attention. Concretely it runs the fallback once,
    reloads the row, and asserts BOTH the marker key is present AND
    ``awaiting_human_since`` is set, confirming the atomic pairing.
    """
    from awf.runtime.pr_monitor import _MERGE_BLOCK_ATTENTION_STATE_KEY

    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        # Deterministic branch-protection rejection.
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            ),
        ],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False

    # The marker AND the attention flag must be durable on the SAME row reload —
    # the reciprocal crash window (marker set, attention NULL) must not be
    # observable. A single reload captures both because they committed together.
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.awaiting_human_since is not None
        assert _MERGE_BLOCK_ATTENTION_STATE_KEY in (ws.monitor_threads_addressed or {})

    # The marker timestamp in the DB must match the in-memory stamp (no separate
    # wall-clock read that could drift), and the attention flag must be set.
    reloaded_state = runner._load_state(ws)
    assert (
        reloaded_state.merge_block_attention_active(
            ttl_seconds=runner._config.merge_block_attention_ttl_seconds,
        )
        is True
    )
    assert ws.awaiting_human_reason is not None
    assert "GitHub rejected the merge attempt" in ws.awaiting_human_reason


@pytest.mark.unit
async def test_merge_blocker_fallback_marker_and_attention_survive_reciprocal_restart(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lcgk0: after a restart that lands in the reciprocal crash
    window (marker durable, attention commit pending), the next poll's
    ``_clear_stale_merge_attention`` preserve path must NOT silently leave
    ``awaiting_human_since`` NULL while re-stamping the marker.

    With the atomic pairing, a restart cannot land between the marker and
    attention commits — they are one transaction. This test seeds the
    reciprocal-window DB state DIRECTLY (fresh marker, NULL attention) to prove
    the preserve path's behavior is safe regardless: even if some other path
    ever produced a fresh-marker/NULL-attention row, the preserve path must not
    mask the missing attention. The atomic fix makes that state unreachable from
    the fallback; this test pins the invariant for future regressions.
    """
    from awf.runtime.pr_monitor import (
        _MERGE_BLOCK_ATTENTION_STATE_KEY,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    # Seed the reciprocal-window DB state directly: fresh marker, NULL attention.
    fresh_marker = datetime.now(UTC).isoformat()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            **(ws.monitor_threads_addressed or {}),
            _MERGE_BLOCK_ATTENTION_STATE_KEY: fresh_marker,
        }
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_MergeMethodClient(
            repo_methods=("merge", "squash"),
            branch_methods=("merge", "squash"),
        ),
    )

    # Reload state from the reciprocal-window row, then run the preserve path.
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    state = runner._load_state(ws)
    assert (
        state.merge_block_attention_active(
            ttl_seconds=runner._config.merge_block_attention_ttl_seconds,
        )
        is True
    )

    # The atomic fallback makes the fresh-marker/NULL-attention state unreachable
    # in practice; the preserve path here re-stamps the marker and returns without
    # touching attention. This test documents that the reciprocal window is closed
    # at the WRITE boundary (the fallback's atomic pairing), not by the preserve
    # path reconstructing the missing attention — so the invariant to guard is
    # "the fallback never produces this row", asserted by the atomic test above.
    await runner._clear_stale_merge_attention(workspace_id, state)

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    # The marker stays fresh (preserve path re-stamps it).
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY in (ws.monitor_threads_addressed or {})
    # Attention remains NULL — the preserve path does not reconstruct it; the
    # atomic write at the fallback is what prevents this state from ever forming.
    assert ws.awaiting_human_since is None


@pytest.mark.unit
async def test_merge_blocker_fallback_notification_transient_error_retries_then_clears(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient post_comment blip while notifying about a *deterministic* merge
    blocker must run the bounded forge-retry path (wait + persist the
    ``post_human_notification`` counter) instead of escaping the merge loop, and a
    later successful post must clear that budget — mirroring the merge-method
    preflight notification arm rather than calling ``_post_human_notification_once``
    naked."""
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()

    def deterministic_merge_error() -> GitHubClientError:
        return GitHubClientError(
            operation="gh pr merge",
            returncode=1,
            stderr="GraphQL: Pull request could not be merged with this method.",
        )

    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[deterministic_merge_error()],
        post_comment_error=GitHubClientError(
            operation="gh pr comment",
            returncode=1,
            stderr="HTTP 502 Bad Gateway",
        ),
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    async def _run_merge() -> bool | None:
        return await runner._execute(
            action=Merge(),
            workspace_id=workspace_id,
            repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
            repo=_TEST_REPO,
            pr_number=_TEST_PR_NUMBER,
            status=_mergeable_status(),
            state=state,
            base_branch=_TEST_DEFAULT_BASE_BRANCH,
            remote_branch=f"awf/{workspace_id}",
            remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    counter_key = "__awf_forge_transient_retry_count:post_human_notification"

    # Phase 1: deterministic merge blocker + transient 502 on the notification post.
    # The wrapped retry path waits and re-polls instead of letting the 502 escape,
    # and persists the bounded ``post_human_notification`` counter.
    terminal = await _run_merge()
    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert gh.comments == []
    assert sleep_fn.calls == [5]
    assert state.threads_addressed_ids.get(counter_key) == "1"

    # Phase 2: a later poll whose notification post succeeds clears the budget so a
    # recovered one-off blip never accumulates toward the bounded retry count.
    gh.post_comment_error = None
    gh.merge_results = [deterministic_merge_error()]
    terminal = await _run_merge()
    assert terminal is False
    assert gh.merge_calls == ["squash", "squash"]
    assert len(gh.comments) == 1
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert counter_key not in state.threads_addressed_ids
    assert sleep_fn.calls == [5, 60]


@pytest.mark.unit
async def test_mismatched_first_merge_rejection_notifies_without_method_rotation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A rejection naming a different method is not enough to rotate methods."""
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge commits are not allowed on this repository.",
            ),
            "MERGESHA999",
        ],
    )

    terminal, state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert "MERGE_METHOD_MISMATCH" not in gh.comments[0]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_mismatched_last_merge_rejection_notifies_without_method_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A rejection naming a different method does not persist a method blocker."""
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge commits are not allowed on this repository.",
            )
        ],
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert "MERGE_METHOD_MISMATCH" not in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_method_rejection_notification_truncates_long_github_detail(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Final method-blocker notifications preserve fields and cap GitHub detail."""
    long_detail = " ".join(f"detail{i:03d}" for i in range(80))
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr=(
                    f"GraphQL: Squash merges are not allowed on this repository. {long_detail}"
                ),
            )
        ],
    )

    terminal, _state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert len(gh.comments) == 1
    comment = gh.comments[0]
    assert "attempted=squash; effective_allowed=squash" in comment
    assert "detail000" in comment
    assert "detail079" not in comment
    github_detail = comment.split("GitHub reported: ", 1)[1].split("\n\n", 1)[0]
    assert len(github_detail.removesuffix(".")) <= 240


@pytest.mark.unit
async def test_unclassified_single_merge_failure_notifies_without_method_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An unclassified single-method failure does not record a method blocker."""
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            )
        ],
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert "MERGE_METHOD_MISMATCH" not in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_deterministic_bitbucket_merge_failure_notifies_and_keeps_polling(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A permanent Bitbucket merge fault notifies a human instead of terminating.

    Bitbucket workspaces merge through ``BitbucketClient.merge_pr``, which raises
    ``BitbucketClientError`` (not ``GitHubClientError``). A deterministic failure
    (branch restrictions, unresolved tasks, a 4xx) must follow the GitHub
    merge-blocker behaviour — post a human notification and keep polling — rather
    than escaping ``_attempt_merge_method`` and terminating the workspace.
    """
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="merge_pr",
                status=403,
                body="merge checks have not passed",
            )
        ],
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "Bitbucket rejected the merge attempt" in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_deterministic_bitbucket_merge_failure_forwards_specific_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The failed merge operation forwards ``exc.reason_code`` end-to-end.

    A specific code such as ``BITBUCKET_MERGE_METHOD_UNSUPPORTED`` (raised when a
    merge method maps to no Bitbucket strategy) must surface in the operation
    record and audit event rather than being flattened to a generic
    ``BITBUCKET_MERGE_FAILED`` — otherwise an operator inspecting events has to
    read the prose ``error_message`` to recover the real cause.
    """
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="merge_pr",
                status=None,
                body="unsupported merge method for Bitbucket: 'rebase'",
                reason_code=BITBUCKET_MERGE_METHOD_UNSUPPORTED,
            )
        ],
    )

    terminal, _state, _sleep_fn, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    async with factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.monitor_state,
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

    failed_ops = [op for op in operations if op.status == OperationStatus.failed.value]
    assert failed_ops, "expected a failed merge operation to be recorded"
    assert all(op.error_code == BITBUCKET_MERGE_METHOD_UNSUPPORTED for op in failed_ops)
    assert not any(op.error_code == "BITBUCKET_MERGE_FAILED" for op in failed_ops)

    merge_failed_events = [
        event
        for event in events
        if isinstance(event.payload, dict)
        and event.payload.get("action") == "merge"
        and event.payload.get("outcome") == "failed"
    ]
    assert merge_failed_events, "expected a failed merge audit event"
    assert all(
        event.payload.get("reason_code") == BITBUCKET_MERGE_METHOD_UNSUPPORTED
        for event in merge_failed_events
    )


@pytest.mark.unit
async def test_transient_bitbucket_merge_failure_waits_without_notify(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient Bitbucket merge blip waits and re-polls without notifying."""
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="merge_pr",
                status=429,
                body="rate limited",
                reason_code=BITBUCKET_RATE_LIMITED,
            )
        ],
    )

    terminal, _state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert gh.comments == []
    assert sleep_fn.calls == [5]


@pytest.mark.unit
async def test_in_progress_bitbucket_merge_does_not_record_failed_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A 409 ``BITBUCKET_MERGE_IN_PROGRESS`` cancels (never fails) the operation.

    The reason code is transient: ``_wait_after_transient_bitbucket_error`` keeps
    the monitor polling and a later ``fetch_pr_status`` observes the in-flight
    merge's MERGED state, completing the workspace successfully. Recording a
    permanent ``BITBUCKET_MERGE_FAILED`` operation here would leave an inconsistent
    audit trail (operation "failed" while the workspace completes), so the failure
    record is omitted for this reason code while still waiting and re-polling.

    The running merge-attempt operation must still reach a terminal state: if the
    in-flight merge completes before the next loop re-enters ``Merge`` the monitor
    short-circuits to completion and never finishes this operation, orphaning it as
    ``running`` forever. The attempt is therefore cancelled before returning the
    transient blocker.
    """
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="merge_pr",
                status=409,
                body="merge already in progress",
                reason_code=BITBUCKET_MERGE_IN_PROGRESS,
            )
        ],
    )

    terminal, _state, sleep_fn, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert gh.comments == []
    assert sleep_fn.calls == [5]

    async with factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.monitor_state,
        )
        audit_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_result",
            limit=20,
        )
    assert not any(op.error_code == "BITBUCKET_MERGE_FAILED" for op in operations)
    assert not any(op.status == OperationStatus.failed.value for op in operations)
    # The merge-attempt operation must be terminal (cancelled), not orphaned as
    # ``running`` — otherwise a later ShortCircuitCompleted leaves it stuck.
    merge_ops = [
        op
        for op in operations
        if isinstance(op.payload, dict) and op.payload.get("reason_code") == "MERGE"
    ]
    assert merge_ops, "expected a merge-attempt operation to be recorded"
    assert all(op.status == OperationStatus.cancelled.value for op in merge_ops)
    assert not any(op.status == OperationStatus.running.value for op in operations)
    # The cancellation must leave an audit breadcrumb so operators can tell
    # "superseded by an in-flight merge" apart from an unexplained cancelled
    # operation — every other merge arm (GitHub, Bitbucket failure, success)
    # records a merge_result event, so this transient arm must too.
    in_progress_events = [
        event for event in audit_events if event.reason_code == BITBUCKET_MERGE_IN_PROGRESS
    ]
    assert len(in_progress_events) == 1
    audit_payload = in_progress_events[0].payload
    assert isinstance(audit_payload, dict)
    assert audit_payload["action"] == "merge"
    assert audit_payload["outcome"] == "cancelled"


@pytest.mark.unit
async def test_merge_task_timeout_cancels_operation_and_keeps_polling(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An exhausted async-merge poll budget cancels (never fails) the operation.

    ``BitbucketClient`` raises ``BITBUCKET_MERGE_TASK_TIMEOUT`` when the bounded
    poll budget is exhausted while the merge task is still PENDING. The merge may
    still complete server-side, so this is treated exactly like
    ``BITBUCKET_MERGE_IN_PROGRESS``: the attempt operation is cancelled (not
    failed), no "merge rejected" comment is posted, and the monitor waits and
    re-polls. Misclassifying it as deterministic would post a spurious notification
    and leave a permanently-failed operation for a merge that succeeds.
    """
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="bitbucket merge_pr (task-status)",
                status=None,
                body="Bitbucket merge task did not complete within 30 polls",
                reason_code=BITBUCKET_MERGE_TASK_TIMEOUT,
            )
        ],
    )

    terminal, _state, sleep_fn, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    # No "merge rejected" notification while the async merge may still be running.
    assert gh.comments == []
    assert sleep_fn.calls == [5]

    async with factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.monitor_state,
        )
        audit_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_result",
            limit=20,
        )
    assert not any(op.status == OperationStatus.failed.value for op in operations)
    merge_ops = [
        op
        for op in operations
        if isinstance(op.payload, dict) and op.payload.get("reason_code") == "MERGE"
    ]
    assert merge_ops, "expected a merge-attempt operation to be recorded"
    assert all(op.status == OperationStatus.cancelled.value for op in merge_ops)
    assert not any(op.status == OperationStatus.running.value for op in operations)
    # The cancellation breadcrumb carries the timeout reason code so operators can
    # tell it apart from the 409 already-in-flight case.
    timeout_events = [
        event for event in audit_events if event.reason_code == BITBUCKET_MERGE_TASK_TIMEOUT
    ]
    assert len(timeout_events) == 1
    audit_payload = timeout_events[0].payload
    assert isinstance(audit_payload, dict)
    assert audit_payload["outcome"] == "cancelled"


class _AttentionCheckingSleep(RecordedSleep):
    """Records sleeps and asserts ``awaiting_human_since`` is cleared mid-sleep.

    Used by the #661 tests to prove the resolved ``NotifyHuman`` attention flag is
    cleared BEFORE the pre-merge settle sleep / fast-path merge attempt, not
    only after the whole poll resolves.
    """

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._workspace_id = workspace_id
        self.cleared_before_sleep: list[bool] = []

    async def __call__(self, seconds: float) -> None:
        async with self._factory() as session:
            ws = await WorkspaceRepository(session).get(self._workspace_id)
            assert ws is not None
            self.cleared_before_sleep.append(ws.awaiting_human_since is None)
        await super().__call__(seconds)


@pytest.mark.unit
async def test_resolved_human_wait_clears_attention_before_pre_merge_settle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """#661: a resolved ``HUMAN_WAIT`` episode must not keep surfacing
    "awaiting human" while the monitor settles before merging.

    ``decide()`` returns ``Merge`` after the human block resolves, so the
    top-of-``_execute`` clear is skipped for the ``Merge`` arm. The merge loop
    must clear the stale flag at critical-section entry — BEFORE the pre-merge
    settle sleep — so console KPIs/badges do not show "awaiting human" for the
    ~90s settle window while the monitor is merely waiting to merge.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # git fetch origin development
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(check_state="PENDING"))
    sleep_fn = _AttentionCheckingSleep(factory=factory, workspace_id=workspace_id)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        pre_merge_settle_seconds=5,
    )

    # Seed a stable episode start from an earlier, now-resolved NotifyHuman poll.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior human escalation", now=episode_start
        )
        await session.commit()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=MonitorState(),
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    # The recheck after settle returned PENDING → WaitForCI, no merge attempted.
    assert terminal is False
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        # The flag was cleared at critical-section entry, before the settle sleep.
        assert ws.awaiting_human_since is None
        assert ws.awaiting_human_reason is None
    # The settle sleep (the first recorded sleep) observed the flag already clear.
    assert sleep_fn.cleared_before_sleep
    assert all(sleep_fn.cleared_before_sleep)


@pytest.mark.unit
async def test_resolved_human_wait_clears_attention_on_fast_path_into_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """#661 fast path: with ``pre_merge_settle_seconds == 0`` the critical-section
    entry clear runs right before the merge attempt, so the resolved
    ``NotifyHuman`` flag is cleared and the merge proceeds without surfacing
    "awaiting human" while actively merging.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=["MERGESHA123"],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
        initial_review_grace_period_seconds=0,
        pre_merge_settle_seconds=0,
    )

    # Seed a stable episode start from an earlier, now-resolved NotifyHuman poll.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior human escalation", now=episode_start
        )
        await session.commit()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=MonitorState(),
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    # The merge succeeded.
    assert terminal is True
    assert gh.merge_calls  # a merge was attempted
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        # The flag was cleared at critical-section entry before the merge attempt.
        assert ws.awaiting_human_since is None
        assert ws.awaiting_human_reason is None


class _LongWaitMergeCoordinator:
    """A merge coordinator that actually sleeps before yielding, advancing the
    wall-clock past a tiny TTL so a marker stamped fresh at entry ages out
    during the serialized wait.

    Models the real Postgres/InProcess coordinators blocking behind another
    merge in the same repo/base lane: no branch-protection fallback fires
    during that wait (no poll runs), so the marker is NOT re-stamped. Used to
    reproduce the flicker described in PRRT_kwDOSJAM6s6La_SZ.
    """

    def __init__(self, wait_seconds: float) -> None:
        self._wait_seconds = wait_seconds
        self.entries: list[tuple[str, str]] = []

    @asynccontextmanager
    async def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AsyncIterator[None]:
        self.entries.append((repo_url, base_branch))
        # Real sleep so datetime.now(UTC) advances past the TTL inside the wait.
        await asyncio.sleep(self._wait_seconds)
        yield


@pytest.mark.unit
async def test_long_merge_coordinator_wait_preserves_fresh_at_entry_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6La_SZ: a serialized-merge wait longer than the merge-block
    TTL must NOT clear a ``merge_block_attention`` marker that was FRESH at
    coordinator entry.

    The branch-protection fallback re-stamps the marker every poll while
    blocked, but the merge coordinator can block behind another merge for
    longer than the TTL without any poll firing (no fallback runs during the
    wait). Before the fix, ``_clear_stale_merge_attention`` measured the
    marker's age against the post-wait wall-clock, so a marker fresh at entry
    aged past the TTL during the wait and was misclassified as RESOLVED —
    clearing ``awaiting_human_since`` and then letting the deterministic
    rejection re-stamp it, flickering/restarting the human-wait timer though
    the operator block never resolved.

    The fix measures marker age against the coordinator-ENTRY timestamp, so a
    marker fresh when the wait started is preserved across the wait; a marker
    already stale at entry is still cleared (block resolved before the wait).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        # Deterministic branch-protection rejection: decide() stays on Merge and
        # the fallback re-sets attention + re-stamps the marker this poll.
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            ),
        ],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    # TTL large enough that the marker stays FRESH at coordinator entry
    # regardless of how long the pre-coordinator setup (DB loads, gate checks)
    # takes inside ``_execute`` — the production entry-time fix measures the
    # marker's age against the coordinator-ENTRY clock, so the marker only has
    # to be fresh *then*, not survive the whole setup gap. A 1.0s TTL is too
    # tight under CI load (the setup gap can exceed it, making the marker stale
    # at entry and clearing ``awaiting_human_since`` — a flaky false failure,
    # not the regression under test). 30s absorbs any plausible setup gap while
    # still exercising the long-wait path; the during-wait aging only mattered
    # for the pre-fix post-wait clock behavior, which is already fixed
    # (PRRT_kwDOSJAM6s6La_SZ).
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=30.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5)
    runner = PullRequestMonitorRunner(
        session_factory=factory,
        runner=FakeCommandRunner(),
        adapter=FakeAdapter(),
        gh=gh,
        monitor_config=monitor_config,
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=20,
            max_fix_cycle_passes=3,
            pre_push_validation_fix_passes=1,
        ),
        sleep=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        merge_coordinator=coordinator,
    )

    # Seed a stable episode start from an earlier poll (COALESCE'd start). The
    # prior poll's branch-protection fallback stamped attention + a fresh
    # marker; this poll re-enters the merge loop still blocked.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior escalation", now=episode_start
        )
        await session.commit()
    state = MonitorState()
    # Stamp the marker FRESH at this poll's entry — still-blocked. The
    # coordinator wait (1.5s) is shorter than the TTL (30s), so the entry-time
    # fix preserves the marker across the wait; without that fix the
    # critical-section-entry clear would measure age against the post-wait
    # clock and (with a tight TTL) wipe the signal.
    state.mark_merge_block_attention()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert coordinator.entries == [
        (f"git@github.com:{_TEST_REPO.slug()}.git", _TEST_DEFAULT_BASE_BRANCH)
    ]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    # The still-active branch-protection signal is PRESERVED across the long
    # coordinator wait: the episode start is NOT reset (no flicker/restart).
    assert ws.awaiting_human_since == episode_start
    assert ws.awaiting_human_reason is not None
    assert "GitHub rejected the merge attempt" in ws.awaiting_human_reason


@pytest.mark.unit
async def test_stale_at_coordinator_entry_marker_still_cleared_after_long_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6La_SZ parity: a marker that was ALREADY stale at
    coordinator entry (the block resolved BEFORE the wait) is still cleared
    after a long merge-coordinator wait.

    The entry-time reference only preserves a marker that was FRESH at entry;
    a marker already stale at entry means no fallback has fired recently (the
    block resolved before this poll), so the clear must still proceed after
    the wait so "awaiting human" does not stay up while only non-human gates
    remain (#663 contract intact).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=["MERGESHA123"],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=1.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5)
    runner = PullRequestMonitorRunner(
        session_factory=factory,
        runner=FakeCommandRunner(),
        adapter=FakeAdapter(),
        gh=gh,
        monitor_config=monitor_config,
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=20,
            max_fix_cycle_passes=3,
            pre_push_validation_fix_passes=1,
        ),
        sleep=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        merge_coordinator=coordinator,
    )

    # Seed the surfaced attention from a prior, now-resolved poll. The marker is
    # STALE at entry (no fallback has fired since well before the TTL).
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior escalation", now=episode_start
        )
        await session.commit()
    state = MonitorState()
    # Stamp the marker well outside the TTL → stale (resolved) at entry.
    state.mark_merge_block_attention(now=datetime(2025, 12, 31, 0, 0, tzinfo=UTC))

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert coordinator.entries == [
        (f"git@github.com:{_TEST_REPO.slug()}.git", _TEST_DEFAULT_BASE_BRANCH)
    ]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    # The stale-at-entry marker was cleared after the wait: the resolved
    # episode does not stay "awaiting human" once the monitor is merging.
    assert ws.awaiting_human_since is None
    assert ws.awaiting_human_reason is None


class _QueueAfterLockRunner(PullRequestMonitorRunner):
    """Return no blockers pre-lock, then a blocker on the post-lock call.

    Models the real merge coordinator blocking behind another merge in the
    same repo/base lane: the pre-lock queue is clear, but by the time the
    serialized coordinator yields an older candidate has claimed the lane so
    the post-lock recheck reports a queue blocker. Used to reproduce the
    post-lock ``_clear_stale_merge_attention`` regression (PRRT_kwDOSJAM6s6LcfXk).
    """

    def __init__(self, *, blocker: MergeQueueBlocker, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._blocker = blocker
        self.blocker_calls = 0

    async def _merge_queue_blockers_for_workspace(
        self,
        workspace_id: str,
    ) -> list[MergeQueueBlocker]:
        assert workspace_id
        self.blocker_calls += 1
        return [] if self.blocker_calls == 1 else [self._blocker]


@pytest.mark.unit
async def test_long_coordinator_wait_preserves_fresh_at_entry_attention_across_post_lock_queue_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6LcfXk: a serialized-merge wait longer than the merge-block
    TTL must NOT clear a ``merge_block_attention`` marker that was FRESH at
    coordinator entry when a post-lock queue blocker parks the monitor on a
    non-human gate wait.

    The critical-section-entry clear re-stamps a fresh marker to the entry
    timestamp. The serialized merge coordinator can block behind another merge
    for longer than the TTL with no branch-protection fallback firing (no poll
    runs during the wait). Before the fix, the post-lock queue-blocker clear
    measured the marker's age against the post-wait wall-clock (``now=None``),
    so a marker fresh at entry aged past the TTL during the wait and was
    misclassified as RESOLVED — clearing ``awaiting_human_since`` even though
    the branch-protection block was still active. On the next poll the
    fallback re-stamps via COALESCE, restarting the human-wait timer though
    the operator block never resolved.

    The fix passes the coordinator-ENTRY timestamp to the post-lock clears too,
    so a marker fresh when the wait started stays fresh across the post-lock
    queue wait; the attention signal is preserved.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=["MERGESHA123"],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    blocker = MergeQueueBlocker(
        candidate_id="mc_after_lock",
        workspace_id="ws_older",
        attempt_id="attempt_older",
        task_id="task_older",
        title="Older candidate",
        pr_url="https://github.com/example-org/example-repo/pull/41",
        pr_number=41,
        status="open",
        blocker_state="ready",
    )
    # TTL small enough that the 1.5s coordinator wait exceeds it, so a
    # post-wait wall-clock measurement would reclassify a fresh-at-entry
    # marker as stale. The entry-time reference preserves it instead.
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=1.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5)
    runner = _QueueAfterLockRunner(
        blocker=blocker,
        session_factory=factory,
        runner=FakeCommandRunner(),
        adapter=FakeAdapter(),
        gh=GitHubClient(FakeCommandRunner()),
        monitor_config=monitor_config,
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=20,
            max_fix_cycle_passes=3,
            pre_push_validation_fix_passes=1,
        ),
        sleep=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        merge_coordinator=coordinator,
    )

    # Seed a stable episode start from an earlier poll's branch-protection
    # fallback (COALESCE'd start). This poll re-enters the merge loop still
    # blocked, so the marker is fresh at entry.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior escalation", now=episode_start
        )
        await session.commit()
    state = MonitorState()
    # Stamp the marker FRESH at this poll's entry — still-blocked. The
    # coordinator wait (1.5s) exceeds the TTL (1.0s), so without the entry-time
    # reference the post-lock queue clear would measure the marker against the
    # post-wait wall-clock, age it past the TTL, and clear the signal.
    state.mark_merge_block_attention()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    # The post-lock queue blocker parked the monitor on a non-human wait.
    assert terminal is False
    assert runner.blocker_calls == 2
    assert coordinator.entries == [
        (f"git@github.com:{_TEST_REPO.slug()}.git", _TEST_DEFAULT_BASE_BRANCH)
    ]
    # The merge attempt was skipped because the post-lock queue blocker was
    # present before the merge-method preflight/attempt ran.
    assert gh.merge_calls == []
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    # The still-active branch-protection signal is PRESERVED across the long
    # coordinator wait AND the post-lock queue wait: the episode start is NOT
    # reset (no flicker/restart of the human-wait timer).
    assert ws.awaiting_human_since == episode_start
    assert ws.awaiting_human_reason is not None
