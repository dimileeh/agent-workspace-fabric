"""Reasonless ``needs_human`` verdict regression coverage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceEventRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState, ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import (
    _needs_human_reason_state_key,
    _sync_needs_human_reason,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
)
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


def _runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    results: list[VerdictResult | Exception],
) -> tuple[object, list[dict[str, object]]]:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    # These verdict-handling tests use the direct invocation seam.  Re-ask
    # isolation itself is covered with real Git worktrees elsewhere; a runner
    # with an actual worktree root must refuse a missing worktree.
    runner._worktrees_root = None  # type: ignore[assignment]
    calls: list[dict[str, object]] = []

    async def _invoke(**kwargs: object) -> VerdictResult:
        calls.append(kwargs)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    runner._invoke_cli_for_verdict_result = _invoke  # type: ignore[method-assign]
    return runner, calls


async def _reason_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[object]:
    async with factory() as session:
        return [
            event
            for event in await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.audit.comment_resolution",
                limit=20,
            )
            if event.reason_code == "NEEDS_HUMAN_REASON_MISSING"
        ]


@pytest.mark.unit
async def test_thread_reasonless_needs_human_reasks_once_and_stores_sanitized_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory, pr_number=46)
    runner, calls = _runner(
        factory,
        tmp_path,
        [
            VerdictResult(verdict="needs_human"),
            VerdictResult(
                verdict="needs_human",
                reason="A maintainer must choose the checkout policy.",
            ),
        ],
    )
    thread = ReviewThread(thread_id="T-checkout", path="src/checkout.py", line=12, body_excerpt="?")
    state = MonitorState()

    verdict = await comments._address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        thread=thread,
        compose_project="awf-test",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=(),
        task_tag=None,
        base_branch="main",
        remote_branch="feature",
        operation_id="op-reask-success",
        operation_type="comment_repair",
    )

    assert verdict == "needs_human"
    assert len(calls) == 2
    assert "PR #46 (example/repo)" in calls[1]["prompt"]
    assert "thread id T-checkout" in calls[1]["prompt"]
    assert "AWF-EVIDENCE> ?" in calls[1]["prompt"]
    assert state.threads_addressed_ids[_needs_human_reason_state_key(thread.thread_id)] == (
        "A maintainer must choose the checkout policy."
    )
    assert await _reason_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_thread_refusal_or_reask_error_keeps_blocking_verdict_and_records_one_audit_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory, pr_number=46)
    runner, calls = _runner(
        factory,
        tmp_path,
        [VerdictResult(verdict="needs_human"), RuntimeError("re-ask failed")],
    )
    thread = ReviewThread(thread_id="T-missing", path="src/missing.py", line=7, body_excerpt="?")
    state = MonitorState(
        threads_addressed_ids={_needs_human_reason_state_key(thread.thread_id): "stale reason"}
    )

    verdict = await comments._address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        thread=thread,
        compose_project="awf-test",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=(),
        task_tag=None,
        base_branch="main",
        remote_branch="feature",
        operation_id="op-reask-missing",
        operation_type="comment_repair",
    )

    assert verdict == "needs_human"
    assert len(calls) == 2
    assert _needs_human_reason_state_key(thread.thread_id) not in state.threads_addressed_ids
    events = await _reason_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["operation_id"] == "op-reask-missing"
    assert events[0].payload["evidence"]["item_id"] == thread.thread_id
    assert events[0].payload["evidence"]["item_kind"] == "thread"


@pytest.mark.unit
@pytest.mark.parametrize(
    "recovery_error",
    [
        ProviderRecoveryRetryError(),
        ProviderRecoveryFallbackError(),
        ProviderRecoveryAuthError(),
        _MonitorAgentServiceRecoveryFailedError("agent service recovery failed"),
        _MonitorAgentServiceRecoverySupersededError("agent service recovery superseded"),
        ComposeExecCleanupError(
            invocation_id="awf-reask-cleanup",
            source="agent",
            label="monitor",
            message="cleanup failed",
        ),
    ],
)
async def test_thread_reask_reraises_loop_recovery_exceptions(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    recovery_error: Exception,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory, pr_number=46)
    runner, calls = _runner(
        factory,
        tmp_path,
        [VerdictResult(verdict="needs_human"), recovery_error],
    )
    thread = ReviewThread(thread_id="T-recovery", path="src/recovery.py", line=7, body_excerpt="?")

    with pytest.raises(type(recovery_error)):
        await comments._address_thread(
            runner,  # type: ignore[arg-type]
            workspace_id=workspace_id,
            repo=RepoRef(owner="example", name="repo"),
            pr_number=46,
            thread=thread,
            compose_project="awf-test",
            compose_file=tmp_path / "compose.yml",
            owned_paths=(),
            task_tag=None,
        )

    assert len(calls) == 2
    assert await _reason_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_review_comment_placeholder_reason_reasks_once_and_syncs_the_response(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory, pr_number=46)
    runner, calls = _runner(
        factory,
        tmp_path,
        [
            VerdictResult(verdict="needs_human", reason='<what you need> and exit."'),
            VerdictResult(verdict="needs_human", reason="A maintainer must choose the API shape."),
        ],
    )
    comment = ReviewComment(comment_id="R-api", body_excerpt="?")
    state = MonitorState()

    result = await comments._address_review_comment_result(
        runner,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        comment=comment,
        compose_project="awf-test",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=(),
        task_tag=None,
        base_branch="main",
        remote_branch="feature",
        operation_id="op-review-reask",
        operation_type="comment_repair",
    )
    _sync_needs_human_reason(state, comment.comment_id, result)

    assert len(calls) == 2
    assert "PR #46 (example/repo)" in calls[1]["prompt"]
    assert "comment id R-api" in calls[1]["prompt"]
    assert "AWF-EVIDENCE> ?" in calls[1]["prompt"]
    assert result.reason == "A maintainer must choose the API shape."
    assert state.threads_addressed_ids[_needs_human_reason_state_key(comment.comment_id)] == (
        "A maintainer must choose the API shape."
    )
    assert await _reason_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_recovered_needs_human_reason_is_redacted_before_return_and_state_storage(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory, pr_number=46)
    secret = "recoveredVerdictSecret987"
    raw_reason = f"A maintainer must decide whether to rotate GITHUB_TOKEN={secret}."
    redacted_reason = "A maintainer must decide whether to rotate GITHUB_TOKEN=<redacted>"
    runner, calls = _runner(
        factory,
        tmp_path,
        [
            VerdictResult(verdict="needs_human"),
            VerdictResult(verdict="needs_human", reason=raw_reason),
        ],
    )
    comment = ReviewComment(comment_id="R-secret", body_excerpt="?")
    state = MonitorState()

    result = await comments._address_review_comment_result(
        runner,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        comment=comment,
        compose_project="awf-test",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=(),
        task_tag=None,
        base_branch="main",
        remote_branch="feature",
        operation_id="op-review-reask-secret",
        operation_type="comment_repair",
    )
    _sync_needs_human_reason(state, comment.comment_id, result)

    assert len(calls) == 2
    assert result.reason == redacted_reason
    assert secret not in result.reason
    assert state.threads_addressed_ids[_needs_human_reason_state_key(comment.comment_id)] == (
        redacted_reason
    )


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ("defer", "false_positive", "fix_committed"))
async def test_other_verdicts_do_not_trigger_the_needs_human_reask(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    verdict: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory, pr_number=46)
    runner, calls = _runner(
        factory,
        tmp_path,
        [VerdictResult(verdict=verdict, reason="unaffected")],  # type: ignore[arg-type]
    )
    comment = ReviewComment(comment_id=f"R-{verdict}", body_excerpt="?")

    result = await comments._address_review_comment_result(
        runner,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        comment=comment,
        compose_project="awf-test",
        compose_file=tmp_path / "compose.yml",
        owned_paths=(),
        task_tag=None,
    )

    assert result.verdict == verdict
    assert len(calls) == 1
    assert await _reason_events(factory, workspace_id) == []
