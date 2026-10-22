"""Operator acknowledgement of unverified AWF resolutions (#1018).

A thread the disposition gate flags (resolved by AWF's identity, no marker) is
acknowledged through the existing operator guide path: a guide carrying an
explicit ``ACK-THREAD <thread id>`` token for the flagged thread records an
``operator_acknowledged`` disposition (a bare mention does not), and the next
gate run posts that acknowledgement — with its marker, without the directive
text — into the resolved thread, after which the cert can follow. The
acknowledgement is bound to the conversation the gate flagged: a reopen or any
new / edited comment retires it, so it never certifies a later resolution.
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
from awf.db.repositories import WorkspaceRepository
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
from awf.runtime.pr_monitor_runner.lifecycle import _load_state
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
_SECOND = "PRRT_test_silent_p2"
_DIRECTIVE = f"ACK-THREAD {_SILENT} handled in a follow-up issue, ping @maintainer"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _SilentAuditForge(DefaultMergeMethodGitHubClient):
    """One thread resolved by AWF's identity with no AWF marker in it.

    Replies AWF posts are served back on later audits (``visible_replies``) until
    a test deletes them, and join the thread's conversation as on the forge;
    ``resolved`` / ``conversation`` let a test reopen the thread or change its
    feedback, ``edit_during_reply`` / ``reopen_during_reply`` edit or reopen it
    while a reply is in flight, and ``reaudit_error`` fails the first audit after
    the next reply.
    """

    def __init__(self, runner: FakeCommandRunner, *, reply_error: ForgeClientError | None = None):
        super().__init__(runner)
        self.reply_error = reply_error
        self.visible_replies: list[str] = []
        self.resolved = True
        self.conversation = "conversation-1"
        self.edit_during_reply: str | None = None
        self.reopen_during_reply = False
        self.reaudit_error: ForgeClientError | None = None
        self._armed_audit_error: ForgeClientError | None = None

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int
    ) -> ReviewThreadAudit:
        del repo, pr_number
        if self._armed_audit_error is not None:
            error, self._armed_audit_error = self._armed_audit_error, None
            raise error
        return ReviewThreadAudit(
            supported=True,
            viewer_login="awf-operator",
            threads=(
                AuditedReviewThread(
                    thread_id=_SILENT,
                    is_resolved=self.resolved,
                    is_outdated=False,
                    resolved_by="awf-operator" if self.resolved else None,
                    viewer_comment_bodies=tuple(self.visible_replies),
                    conversation_digest="|".join([self.conversation, *self.visible_replies]),
                    conversation_digest_before_reply="|".join(
                        [self.conversation, *self.visible_replies[:-1]]
                    )
                    if self.visible_replies
                    else "",
                ),
            ),
        )

    async def reply_to_thread(self, *, thread_id: str, body: str) -> str:
        if self.reply_error is not None:
            raise self.reply_error
        if self.edit_during_reply is not None:
            self.conversation, self.edit_during_reply = self.edit_during_reply, None
        if self.reopen_during_reply:
            self.resolved, self.reopen_during_reply = False, False
        self._armed_audit_error, self.reaudit_error = self.reaudit_error, None
        self.visible_replies.append(body)
        return await super().reply_to_thread(thread_id=thread_id, body=body)


class _TwoSilentAuditForge(DefaultMergeMethodGitHubClient):
    """Two threads resolved by AWF's identity with no AWF marker, audited in order.

    Replies join their thread's conversation as on the forge; ``edit_on_reply``
    maps a thread to the one whose feedback a reviewer edits while AWF's reply
    to it is in flight. ``audits`` counts the audits served.
    """

    def __init__(self, runner: FakeCommandRunner):
        super().__init__(runner)
        self.conversations = {_SILENT: "conversation-1", _SECOND: "conversation-2"}
        self.visible_replies: dict[str, list[str]] = {_SILENT: [], _SECOND: []}
        self.edit_on_reply: dict[str, str] = {}
        self.audits = 0

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int
    ) -> ReviewThreadAudit:
        del repo, pr_number
        self.audits += 1
        return ReviewThreadAudit(
            supported=True,
            viewer_login="awf-operator",
            threads=tuple(self._thread(thread_id) for thread_id in (_SILENT, _SECOND)),
        )

    def _thread(self, thread_id: str) -> AuditedReviewThread:
        replies = self.visible_replies[thread_id]
        conversation = self.conversations[thread_id]
        return AuditedReviewThread(
            thread_id=thread_id,
            is_resolved=True,
            is_outdated=False,
            resolved_by="awf-operator",
            viewer_comment_bodies=tuple(replies),
            conversation_digest="|".join([conversation, *replies]),
            conversation_digest_before_reply="|".join([conversation, *replies[:-1]])
            if replies
            else "",
        )

    async def reply_to_thread(self, *, thread_id: str, body: str) -> str:
        edited = self.edit_on_reply.pop(thread_id, None)
        if edited is not None:
            self.conversations[edited] += "-edited"
        self.visible_replies[thread_id].append(body)
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
        status=_green_status(),
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


async def test_posted_acknowledgement_reply_survives_a_restart_before_the_loop_flush(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A crash, cancellation or ``_execute`` fault after the reply lands skips the
    # loop's ``_persist_state``; the next poll reloads the row, so the reply id
    # must already be on it or the gate posts a second acknowledgement
    # (PRRT_kwDOSJAM6s6vZKxY). Only that record is written (#305).
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    await _gate(runner, workspace_id, state)
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    await runner._persist_state(workspace_id, state)  # the guide cycle's flush
    state.mark_addressed("PRRT_test_unconfirmed", "fix_committed")

    assert (await _gate(runner, workspace_id, state)).blocker is None
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    reloaded = _load_state(None, ws)

    ack = read_item_disposition(reloaded, _SILENT)
    assert ack is not None and ack.reply_comment_id
    assert "PRRT_test_unconfirmed" not in reloaded.threads_addressed_ids
    again = await _gate(runner, workspace_id, reloaded)
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


async def test_acknowledgement_of_a_conversation_that_moved_on_is_dropped_not_posted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    # A guide acknowledges the flagged resolution; before the gate posts it, the
    # thread is reopened with new feedback and silently resolved again.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    gh.conversation = "conversation-2"

    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert gh.thread_replies == []
    assert read_item_disposition(state, _SILENT) is None
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]

    # Only a guide acknowledging the current resolution certifies it.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    passed = await _gate(runner, workspace_id, state)
    assert passed.blocker is None
    assert len(gh.thread_replies) == 1


async def test_acknowledgement_posted_after_an_unseen_edit_is_not_accepted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    # The reviewer edits the feedback after the gate's audit, before the reply
    # lands: the reply then follows feedback the operator never acknowledged.
    gh.edit_during_reply = "conversation-1-edited"

    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert blocked.count_rows is None
    assert len(gh.thread_replies) == 1
    # The edit retires the acknowledgement although its reply is posted.
    assert read_item_disposition(state, _SILENT) is None
    # Posted after the edit, the reply looks current by time; later audits still
    # refuse it, and nothing posts it again.
    again = await _gate(runner, workspace_id, state)
    assert again.blocker is not None and _SILENT in again.blocker
    assert len(gh.thread_replies) == 1

    # A guide acknowledging the edited conversation certifies it with its own reply.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    passed = await _gate(runner, workspace_id, state)
    assert passed.blocker is None
    assert passed.count_rows is not None
    assert dict(passed.count_rows)["operator acknowledged"] == 1
    assert len(gh.thread_replies) == 2


async def test_acknowledgements_count_only_on_the_audit_after_every_reply(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _TwoSilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    flagged = await _gate(runner, workspace_id, state)
    assert flagged.blocker is not None
    assert _SILENT in flagged.blocker and _SECOND in flagged.blocker
    await _consume_guide(
        runner,
        workspace_id,
        state,
        monkeypatch,
        directive=f"ACK-THREAD {_SILENT} ACK-THREAD {_SECOND}",
    )
    # The reviewer edits the first thread's feedback while the second reply is
    # in flight, after the first reply landed (PRRT_kwDOSJAM6s6vW9qA).
    gh.edit_on_reply[_SECOND] = _SILENT
    audits_before = gh.audits

    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert _SECOND not in blocked.blocker
    assert blocked.count_rows is None
    assert [thread_id for thread_id, _ in gh.thread_replies] == [_SILENT, _SECOND]
    # One audit before the replies, and ONE complete audit after the last of them.
    assert gh.audits - audits_before == 2

    # A guide acknowledging the edited conversation certifies both on the audit
    # after its reply; the second thread's reply is not posted again.
    await _consume_guide(
        runner, workspace_id, state, monkeypatch, directive=f"ACK-THREAD {_SILENT}"
    )
    passed = await _gate(runner, workspace_id, state)
    assert passed.blocker is None
    assert passed.count_rows is not None
    assert dict(passed.count_rows)["operator acknowledged"] == 2
    assert len(gh.thread_replies) == 3


async def test_flags_bind_the_conversation_the_audit_after_the_last_reply_saw(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _TwoSilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    flagged = await _gate(runner, workspace_id, state)
    assert flagged.blocker is not None
    assert _SILENT in flagged.blocker and _SECOND in flagged.blocker
    await _consume_guide(
        runner, workspace_id, state, monkeypatch, directive=f"ACK-THREAD {_SECOND}"
    )
    # A reviewer edits the still-flagged thread's feedback while AWF's reply to
    # the acknowledged one is in flight. The cycle decides on the audit after
    # that reply only, so the new flag answers the edited conversation.
    gh.edit_on_reply[_SECOND] = _SILENT

    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert _SECOND not in blocked.blocker

    # One guide acknowledging what the operator was shown certifies it.
    await _consume_guide(
        runner, workspace_id, state, monkeypatch, directive=f"ACK-THREAD {_SILENT}"
    )
    passed = await _gate(runner, workspace_id, state)
    assert passed.blocker is None
    assert passed.count_rows is not None
    assert dict(passed.count_rows)["operator acknowledged"] == 2
    assert [thread_id for thread_id, _ in gh.thread_replies] == [_SECOND, _SILENT]


async def test_re_acknowledgement_right_after_a_rejected_reply_binds_the_re_audited_thread(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    gh.edit_during_reply = "conversation-1-edited"
    assert (await _gate(runner, workspace_id, state)).blocker is not None

    # The operator re-acknowledges at once, with no gate run in between: the
    # flag they answer must already be the conversation the re-audit saw, or
    # the gate drops the guide as stale behind the same, deduped blocker.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    passed = await _gate(runner, workspace_id, state)

    assert passed.blocker is None
    assert passed.count_rows is not None
    assert dict(passed.count_rows)["operator acknowledged"] == 1
    assert len(gh.thread_replies) == 2


async def test_acknowledgement_is_not_accepted_when_the_re_audit_misses_the_thread(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    post_reply = gh.reply_to_thread

    async def _reply_then_vanish(*, thread_id: str, body: str) -> str:
        reply_id = await post_reply(thread_id=thread_id, body=body)

        async def _without_threads(*, repo: RepoRef, pr_number: int) -> ReviewThreadAudit:
            del repo, pr_number
            return ReviewThreadAudit(supported=True, viewer_login="awf-operator")

        monkeypatch.setattr(gh, "fetch_review_thread_audit", _without_threads)
        return reply_id

    monkeypatch.setattr(gh, "reply_to_thread", _reply_then_vanish)

    blocked = await _gate(runner, workspace_id, state)

    # Nothing proves the reply followed the acknowledged conversation: fail closed.
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert len(gh.thread_replies) == 1


async def test_acknowledgement_is_not_accepted_once_the_re_audit_finds_a_reopen(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    # The reviewer reopens the thread while the reply is in flight: the
    # acknowledged resolution no longer stands, whatever the reply says.
    gh.reopen_during_reply = True

    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert blocked.count_rows is None
    assert len(gh.thread_replies) == 1


async def test_acknowledgement_is_unverified_until_a_re_audit_confirms_it(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    gh.reaudit_error = GitHubClientError(
        operation="gh api graphql", returncode=1, stderr="HTTP 502"
    )

    blocked = await _gate(runner, workspace_id, state)

    # The reply landed, but nothing proved it followed the acknowledged
    # conversation: fail closed for this run.
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert len(gh.thread_replies) == 1

    # The next audit proves it: no new guide, no second reply.
    passed = await _gate(runner, workspace_id, state)
    assert passed.blocker is None
    assert len(gh.thread_replies) == 1
    assert _disposition_unverified_key(_SILENT) not in state.threads_addressed_ids


async def test_delayed_guide_after_reopen_cannot_acknowledge_the_old_resolution(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    gh.resolved = False  # the reviewer reopened the thread
    assert (await _gate(runner, workspace_id, state)).blocker is None
    assert _disposition_unverified_key(_SILENT) not in state.threads_addressed_ids

    # A guide written against the old blocker arrives late; the thread is then
    # silently resolved again over the same conversation.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    gh.resolved = True

    assert read_item_disposition(state, _SILENT) is None
    blocked = await _gate(runner, workspace_id, state)
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert gh.thread_replies == []


async def test_reopen_retires_an_acknowledgement_not_yet_posted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    # A guide acknowledges the flagged resolution; before the gate posts it, the
    # reviewer reopens the thread.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    gh.resolved = False

    assert (await _gate(runner, workspace_id, state)).blocker is None
    assert read_item_disposition(state, _SILENT) is None

    # A later silent resolve over the same conversation is not the one acknowledged.
    gh.resolved = True
    blocked = await _gate(runner, workspace_id, state)
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert gh.thread_replies == []

    # Only a guide acknowledging the current resolution certifies it.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    passed = await _gate(runner, workspace_id, state)
    assert passed.blocker is None
    assert len(gh.thread_replies) == 1


async def test_reopen_retires_a_posted_acknowledgement(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    assert (await _gate(runner, workspace_id, state)).blocker is None
    assert len(gh.thread_replies) == 1
    # The reviewer reopens the acknowledged thread without commenting.
    gh.resolved = False

    assert (await _gate(runner, workspace_id, state)).blocker is None
    assert read_item_disposition(state, _SILENT) is None

    # A later silent resolve keeps the old reply, but its marker answered the
    # resolution the reopen ended (PRRT_kwDOSJAM6s6vW9p0).
    gh.resolved = True
    blocked = await _gate(runner, workspace_id, state)
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert len(gh.thread_replies) == 1

    # Only a guide acknowledging the current resolution certifies it, with a fresh reply.
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    passed = await _gate(runner, workspace_id, state)
    assert passed.blocker is None
    assert passed.count_rows is not None
    assert dict(passed.count_rows)["operator acknowledged"] == 1
    assert len(gh.thread_replies) == 2


async def test_reopen_seen_by_the_re_audit_retires_the_posted_acknowledgement(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    gh.reopen_during_reply = True
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    assert read_item_disposition(state, _SILENT) is None

    # Silently resolved again before the next gate run: the posted reply answered
    # the resolution the reopen ended, so it certifies nothing.
    gh.resolved = True
    blocked = await _gate(runner, workspace_id, state)
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert len(gh.thread_replies) == 1

    await _consume_guide(runner, workspace_id, state, monkeypatch)
    passed = await _gate(runner, workspace_id, state)
    assert passed.blocker is None
    assert len(gh.thread_replies) == 2


async def test_acknowledgement_bound_to_no_conversation_is_dropped_not_posted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    # A flag recorded without its conversation binds the acknowledgement to none.
    state.mark_addressed(
        _disposition_unverified_key(_SILENT), "REVIEW_THREAD_DISPOSITION_UNVERIFIED"
    )
    await _consume_guide(runner, workspace_id, state, monkeypatch)
    ack = read_item_disposition(state, _SILENT)
    assert ack is not None and ack.conversation_digest is None

    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert gh.thread_replies == []
    assert read_item_disposition(state, _SILENT) is None


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
