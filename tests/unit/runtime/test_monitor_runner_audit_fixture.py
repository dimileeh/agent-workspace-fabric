"""Shared forge fixtures supply actual thread contents, never empty success."""

from dataclasses import replace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.runtime.pr_monitor import ReviewThread, ReviewThreadComment
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient

pytestmark = pytest.mark.unit
_REPO = RepoRef(owner="acme", name="widgets")


async def test_audit_fixture_retains_comments_replies_and_later_feedback() -> None:
    fake = DefaultMergeMethodGitHubClient(FakeCommandRunner())
    thread = ReviewThread(
        thread_id="PRRT_target",
        path="example.py",
        line=1,
        body_excerpt="question",
        author="reviewer",
        comments=(ReviewThreadComment(comment_id="c1", body="question", author="reviewer"),),
    )
    fake.remember_review_threads((thread,))
    reply = await fake.reply_to_thread(thread_id=thread.thread_id, body="answer")
    first = await fake.fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert first.threads[0].external_comment_bodies == ("question",)
    assert first.threads[0].viewer_comment_ids == (reply,)
    assert first.threads[0].current_viewer_comment_bodies == ("answer",)
    assert first.threads[0].conversation_digest_before_reply

    fake.remember_review_threads(
        (
            replace(
                thread,
                comments=thread.comments
                + (ReviewThreadComment(comment_id="c2", body="follow-up", author="reviewer"),),
            ),
        )
    )
    later = await fake.fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert later.threads[0].external_comment_bodies == ("question", "follow-up")
    assert later.threads[0].current_viewer_comment_bodies == ()
    assert later.threads[0].conversation_digest != first.threads[0].conversation_digest


async def test_audit_fixture_filters_real_posted_threads_but_never_invents_a_missing_one() -> None:
    fake = DefaultMergeMethodGitHubClient(FakeCommandRunner())
    reply = await fake.reply_to_thread(thread_id="PRRT_target", body="answer")
    await fake.reply_to_thread(thread_id="PRRT_other", body="other answer")
    result = await fake.fetch_review_thread_audit(repo=_REPO, pr_number=7, thread_id="PRRT_target")
    assert [t.thread_id for t in result.threads] == ["PRRT_target"]
    assert result.threads[0].viewer_comment_ids == (reply,)
    missing = await fake.fetch_review_thread_audit(
        repo=_REPO, pr_number=7, thread_id="PRRT_missing"
    )
    assert missing.supported and missing.threads == ()


@pytest.mark.parametrize("resolve_succeeds", [True, False])
async def test_audit_fixture_records_only_successful_resolutions(resolve_succeeds: bool) -> None:
    cmd = FakeCommandRunner()
    fake = DefaultMergeMethodGitHubClient(cmd)
    thread = ReviewThread(
        thread_id="PRRT_target",
        path="example.py",
        line=1,
        body_excerpt="question",
        author="reviewer",
        comments=(ReviewThreadComment(comment_id="c1", body="question", author="reviewer"),),
    )
    fake.remember_review_threads((thread,))
    reply = await fake.reply_to_thread(thread_id=thread.thread_id, body="answer")
    cmd.queue_result(
        returncode=0 if resolve_succeeds else 1,
        stdout='{"data":{"resolveReviewThread":{"thread":{"isResolved":true}}}}',
        stderr="" if resolve_succeeds else "missing resolve permission",
    )
    if resolve_succeeds:
        await fake.resolve_thread(thread_id=thread.thread_id)
    else:
        with pytest.raises(GitHubClientError, match="missing resolve permission"):
            await fake.resolve_thread(thread_id=thread.thread_id)

    for thread_id in (None, thread.thread_id):
        audit = await fake.fetch_review_thread_audit(repo=_REPO, pr_number=7, thread_id=thread_id)
        assert len(audit.threads) == 1
        observed = audit.threads[0]
        assert observed.is_resolved is resolve_succeeds
        assert observed.resolved_by == (audit.viewer_login if resolve_succeeds else None)
        assert observed.external_comment_bodies == ("question",)
        assert observed.viewer_comment_ids == (reply,)
        assert observed.current_viewer_comment_bodies == ("answer",)
    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:3] == ["gh", "api", "graphql"]
    assert f"threadId={thread.thread_id}" in cmd.calls[0].args

    # A later observed reopen still overrides the successful mutation.
    fake.remember_review_threads((thread,))
    reopened = await fake.fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert not reopened.threads[0].is_resolved
    assert reopened.threads[0].viewer_comment_ids == (reply,)
