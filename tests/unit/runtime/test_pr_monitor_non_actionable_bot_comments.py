"""Regression tests for bot review feedback routing.

AWF should not decide that review-bot text is semantically ignorable. It
should package current PR feedback for the coding agent and let the agent
decide whether to fix, mark false-positive, or defer. Only AWF's own
bookkeeping comments are filtered before the monitor decision loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import AddressComments, Merge, MonitorState, decide
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    issue_comment_node,
    make_runner,
    pr_payload,
    review_node,
    seed_monitoring_workspace,
)

REPO_URL = "git@github.com:dimileeh/aira-web.git"
REPO = RepoRef.from_url(REPO_URL)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _disabled_review_boilerplate() -> dict:
    return review_node(
        cid=7801,
        author="coderabbitai",
        body=(
            "> [!IMPORTANT]\n"
            "> ## Review skipped\n\n"
            "Auto reviews are disabled on base/target branches other than "
            "the configured development branch.\n\n"
            "- [ ] Trigger review"
        ),
    )


def _disabled_issue_comment_boilerplate() -> dict:
    return issue_comment_node(
        cid=7803,
        author="coderabbitai",
        body=(
            "> [!IMPORTANT]\n"
            "> ## Review skipped\n\n"
            "Auto reviews are disabled on base/target branches other than "
            "the configured development branch.\n\n"
            "- [ ] Trigger review"
        ),
    )


def _late_actionable_review() -> dict:
    return review_node(
        cid=7802,
        author="human-reviewer",
        body="late actionable review: document the monitor behavior before merging.",
    )


def _actionable_codex_issue_comment() -> dict:
    return issue_comment_node(
        cid=7804,
        author="chatgpt-codex-connector[bot]",
        body=(
            "\n### Codex Review\n\n"
            "https://github.com/dimileeh/agent-workspace-fabric/"
            "blob/49c0c400de80f2b7ffb4f67bb6a76868f4d0e6ae/"
            "src/awf/runtime/pr_monitor_runner.py#L940-L941\n"
            "**P2 Preserve action-specific base-fetch retry counts**\n\n"
            "When `sync_base` keeps hitting a transient `BaseFetchError`, "
            "clear only the successful context's counter."
        ),
    )


@pytest.mark.unit
async def test_bot_review_boilerplate_routes_to_agent_without_human_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(reviews=[_disabled_review_boilerplate()]),
    )
    cmd.queue_result(returncode=0)  # gh pr merge
    cmd.queue_result(returncode=0, stdout="merge-sha\n")  # merge commit lookup
    cmd.queue_result(returncode=0)  # docker compose down
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=0,
    )

    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    action = decide(status, MonitorState(), runner._config)

    assert isinstance(action, AddressComments)
    assert action.threads == ()
    assert [c.comment_id for c in action.review_comments] == ["7801"]
    assert action.review_comments[0].blocks_merge is False
    assert adapter.calls == []


@pytest.mark.unit
async def test_bot_issue_boilerplate_defer_does_not_notify_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(comments=[_disabled_issue_comment_boilerplate()]),
    )
    cmd.queue_result(returncode=0)  # gh pr merge
    cmd.queue_result(returncode=0, stdout="merge-sha\n")  # merge commit lookup
    cmd.queue_result(returncode=0)  # docker compose down
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=0,
    )

    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    state = MonitorState(threads_addressed_ids={"issue:7803": "defer"})
    action = decide(status, state, runner._config)

    assert isinstance(action, Merge)
    assert status.unresolved_review_comments[0].comment_id == "issue:7803"
    assert status.unresolved_review_comments[0].blocks_merge is False
    assert adapter.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["false_positive", "fix_committed"])
async def test_handled_bot_issue_policy_blocker_does_not_notify_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    verdict: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(comments=[_disabled_issue_comment_boilerplate()]),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=0,
    )

    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    state = MonitorState(threads_addressed_ids={"issue:7803": verdict})
    action = decide(status, state, runner._config)

    assert isinstance(action, Merge)
    assert status.unresolved_review_comments[0].comment_id == "issue:7803"
    assert status.unresolved_review_comments[0].blocks_merge is False
    assert adapter.calls == []


@pytest.mark.unit
async def test_actionable_bot_issue_comment_routes_to_address_comments(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(comments=[_actionable_codex_issue_comment()]),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=0,
    )

    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    action = decide(status, MonitorState(), runner._config)

    assert isinstance(action, AddressComments)
    assert action.threads == ()
    assert [c.comment_id for c in action.review_comments] == ["issue:7804"]
    assert "Preserve action-specific base-fetch retry counts" in (
        action.review_comments[0].body_excerpt
    )


@pytest.mark.unit
async def test_bot_review_body_during_initial_grace_routes_to_agent_before_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(reviews=[_disabled_review_boilerplate()]),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=900,
    )
    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    state = MonitorState()
    action = decide(status, state, runner._config)
    assert isinstance(action, AddressComments)
    assert [c.comment_id for c in action.review_comments] == ["7801"]
    assert sleep_fn.calls == []


@pytest.mark.unit
async def test_bot_boilerplate_and_later_actionable_review_route_to_address_comments(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    disabled = _disabled_review_boilerplate()
    actionable = _late_actionable_review()
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(reviews=[disabled, actionable]))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=900,
    )

    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    action = decide(status, MonitorState(), runner._config)

    assert isinstance(action, AddressComments)
    assert [c.comment_id for c in action.review_comments] == ["7801", "7802"]
    assert "Auto reviews are disabled" in action.review_comments[0].body_excerpt
    assert "late actionable review" in action.review_comments[1].body_excerpt
    assert adapter.calls == []


@pytest.mark.unit
async def test_bot_boilerplate_does_not_create_policy_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(reviews=[_disabled_review_boilerplate()]),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=900,
    )
    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    state = MonitorState()
    action = decide(status, state, runner._config)
    assert isinstance(action, AddressComments)
    assert [c.blocks_merge for c in action.review_comments] == [False]
