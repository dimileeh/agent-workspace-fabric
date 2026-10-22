"""Operator acknowledgement of unverified AWF resolutions (#1018).

A thread the disposition gate flags (resolved by AWF's identity, no marker) is
acknowledged through the existing operator guide path: a guide carrying an
explicit ``ACK-THREAD <thread id>`` token for the flagged thread records an
``operator_acknowledged`` disposition (a bare mention does not), and the next
gate run posts that acknowledgement — with its marker, without the directive
text — into the resolved thread, after which the cert can follow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedReviewThread, ReviewThreadAudit
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _disposition_unverified_key
from awf.runtime.operator_hints import mark_operator_hint_needs_human
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_actions import AddressOperatorHint
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.thread_disposition import (
    acknowledged_thread_ids,
    read_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_gate import (
    review_thread_disposition_gate,
)
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._thread_disposition_fixtures import settled_baseline_state

pytestmark = pytest.mark.unit

_REPO = RepoRef(owner="acme", name="widgets")
_SILENT = "PRRT_test_silent_p1"
_DIRECTIVE = f"ACK-THREAD {_SILENT} handled in a follow-up issue, ping @maintainer"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _SilentAuditForge(DefaultMergeMethodGitHubClient):
    """One thread resolved by AWF's identity with no AWF marker in it.

    Replies AWF posts are served back on later audits (``visible_replies``) until
    a test deletes them.
    """

    def __init__(self, runner: FakeCommandRunner, *, reply_error: ForgeClientError | None = None):
        super().__init__(runner)
        self.reply_error = reply_error
        self.visible_replies: list[str] = []

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int
    ) -> ReviewThreadAudit:
        del repo, pr_number
        return ReviewThreadAudit(
            supported=True,
            viewer_login="awf-operator",
            threads=(
                AuditedReviewThread(
                    thread_id=_SILENT,
                    is_resolved=True,
                    is_outdated=False,
                    resolved_by="awf-operator",
                    viewer_comment_bodies=tuple(self.visible_replies),
                ),
            ),
        )

    async def reply_to_thread(self, *, thread_id: str, body: str) -> str:
        if self.reply_error is not None:
            raise self.reply_error
        self.visible_replies.append(body)
        return await super().reply_to_thread(thread_id=thread_id, body=body)


async def _runner(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, gh: Any
) -> tuple[str, Any]:
    workspace_id = await seed_monitoring_workspace(factory, auto_merge=False)
    runner = make_runner(
        factory=factory,
        cmd=gh._runner,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        gh=gh,
    )
    return workspace_id, runner


async def _gate(runner: Any, workspace_id: str, state: MonitorState) -> Any:
    return await review_thread_disposition_gate(
        runner,
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        monitor_log=None,
    )


def _guide(directive: str) -> OperatorHint:
    return OperatorHint(
        reason="guide",
        directive=directive,
        operation_id="op_test_1",
        reason_code="OPERATOR_GUIDE",
    )


async def _consume_guide(
    runner: Any,
    workspace_id: str,
    state: MonitorState,
    monkeypatch: pytest.MonkeyPatch,
    *,
    directive: str = _DIRECTIVE,
    agent_outcome: str = "processed",
) -> None:
    """Run one operator-hint action whose agent cycle ends ``agent_outcome``.

    The coding-agent cycle is replaced: the acknowledgement must not depend on
    what the agent answered for the rest of the directive.
    """
    hint = _guide(directive)
    state.pending_operator_hint = hint

    async def _cycle(**_kwargs: object) -> _GitPushResult:
        if agent_outcome == "needs_human":
            mark_operator_hint_needs_human(state, "the agent could not act on the directive")
        else:
            _finalize_processed_operator_hint(state, hint=hint)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _cycle)
    await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url="git@github.com:acme/widgets.git",
        repo=_REPO,
        pr_number=42,
        status=_green_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
        monitor_log=None,
    )


def _green_status() -> PRStatus:
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


@pytest.mark.parametrize("agent_outcome", ["processed", "needs_human"])
async def test_operator_guide_acknowledges_flagged_thread_and_cert_follows(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_outcome: str,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    blocked = await _gate(runner, workspace_id, state)
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]

    # The guide is the acknowledgement, whatever its agent run answered.
    await _consume_guide(runner, workspace_id, state, monkeypatch, agent_outcome=agent_outcome)
    ack = read_item_disposition(state, _SILENT)
    assert ack is not None and ack.disposition_class == "operator_acknowledged"
    assert _disposition_unverified_key(_SILENT) not in state.threads_addressed_ids

    passed = await _gate(runner, workspace_id, state)

    assert passed.blocker is None
    assert passed.count_rows is not None
    assert dict(passed.count_rows)["operator acknowledged"] == 1
    assert len(gh.thread_replies) == 1
    thread_id, body = gh.thread_replies[0]
    assert thread_id == _SILENT
    assert body.splitlines()[0] == (
        f"<!-- AWF:thread-disposition:v1 class=operator_acknowledged thread={_SILENT} -->"
    )
    assert "op_test_1" in body
    assert "follow-up issue" not in body  # the directive text is never published
    assert "@" not in body

    # Later audits find the posted reply's marker: no second post.
    again = await _gate(runner, workspace_id, state)
    assert again.blocker is None
    assert len(gh.thread_replies) == 1


async def test_deleted_acknowledgement_reply_blocks_until_re_acknowledged(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    await _gate(runner, workspace_id, state)
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    assert (await _gate(runner, workspace_id, state)).blocker is None
    gh.visible_replies.clear()  # the acknowledgement reply was deleted from the thread

    blocked = await _gate(runner, workspace_id, state)

    # The saved reply id prevents a duplicate post; it is not evidence on its own.
    assert blocked.blocker is not None
    assert blocked.blocker.startswith("REVIEW_THREAD_DISPOSITION_UNVERIFIED:")
    assert _SILENT in blocked.blocker
    assert blocked.count_rows is None
    assert len(gh.thread_replies) == 1
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]

    # A fresh guide re-acknowledges the flagged thread and the reply is re-posted.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    restored = await _gate(runner, workspace_id, state)
    assert restored.blocker is None
    assert len(gh.thread_replies) == 2


async def test_acknowledgement_reply_failure_keeps_the_thread_blocked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(
        FakeCommandRunner(),
        reply_error=GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502"),
    )
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    await _gate(runner, workspace_id, state)
    await _consume_guide(runner, workspace_id, state, monkeypatch)

    result = await _gate(runner, workspace_id, state)

    assert result.blocker is not None
    assert result.blocker.startswith("REVIEW_THREAD_DISPOSITION_UNVERIFIED:")
    assert _SILENT in result.blocker
    assert gh.thread_replies == []
    ack = read_item_disposition(state, _SILENT)
    assert ack is not None and ack.reply_comment_id is None


async def test_guide_naming_unflagged_thread_records_nothing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    await _consume_guide(runner, workspace_id, state, monkeypatch)
    assert read_item_disposition(state, _SILENT) is None

    result = await _gate(runner, workspace_id, state)
    assert result.blocker is not None
    assert gh.thread_replies == []


@pytest.mark.parametrize(
    "directive",
    [
        f"Acknowledge {_SILENT}: handled in a follow-up issue",
        f"Re-check {_SILENT} and fix it properly",
        f"ack-thread {_SILENT}",
        f"NOACK-THREAD {_SILENT}",
        f"ACK-THREAD: {_SILENT}",
    ],
    ids=["acknowledge-word", "plain-mention", "lowercase", "prefixed", "colon"],
)
async def test_guide_mentioning_flagged_thread_without_ack_token_records_nothing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directive: str,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None

    await _consume_guide(runner, workspace_id, state, monkeypatch, directive=directive)

    assert read_item_disposition(state, _SILENT) is None
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    assert gh.thread_replies == []


def test_acknowledged_thread_ids_reads_only_explicit_tokens() -> None:
    text = (
        "ACK-THREAD PRRT_test_a\n"
        "also ACK-THREAD\tPRRT_test_b, and ACK-THREAD PRRT_test_a again; "
        "see PRRT_test_c, ack-thread PRRT_test_d, XACK-THREAD PRRT_test_e"
    )
    assert acknowledged_thread_ids(text) == ("PRRT_test_a", "PRRT_test_b")
    assert acknowledged_thread_ids("") == ()
