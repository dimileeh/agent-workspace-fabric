"""BitbucketClient disposition reply + (unsupported) resolution audit (#1018).

A ``bb:`` inline comment thread gets a child comment (``parent.id``); a
``bbtask:`` reviewer task has no reply thread, so its disposition is posted as a
PR comment. The resolver-identity audit is reported unsupported rather than
guessed.
"""

from __future__ import annotations

import json

import pytest

from awf.common.bitbucket_client import BitbucketClientError

from ._helpers import FakeBitbucket, RecordingSleep, make_client, repo

pytestmark = pytest.mark.unit

_REPO = "/2.0/repositories/workspace/repo"
_PR = f"{_REPO}/pullrequests/42"


async def test_reply_to_comment_thread_posts_child_comment() -> None:
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_PR}/comments", json={"id": 501})
    client = make_client(fake)
    reply_id = await client.reply_to_thread(thread_id="bb:workspace/repo#42:100", body="d")
    call = fake.calls("POST")[0]
    assert call.url.path == f"{_PR}/comments"
    assert json.loads(call.content) == {"content": {"raw": "d"}, "parent": {"id": 100}}
    assert reply_id == "501"


async def test_reply_to_task_posts_top_level_comment() -> None:
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_PR}/comments", json={"id": 502})
    client = make_client(fake)
    reply_id = await client.reply_to_thread(thread_id="bbtask:workspace/repo#42:9", body="t")
    call = fake.calls("POST")[0]
    assert call.url.path == f"{_PR}/comments"
    assert json.loads(call.content) == {"content": {"raw": "t"}}
    assert reply_id == "502"


@pytest.mark.parametrize(
    ("status", "headers"),
    [(200, {"X-RateLimit-NearLimit": "true"}), (429, {"Retry-After": "7"})],
    ids=["near-limit-after-success", "rate-limited"],
)
async def test_reply_never_sleeps_or_retries(status: int, headers: dict[str, str]) -> None:
    # PRRT_kwDOSJAM6s6vbqy5: a disposition reply is a non-idempotent mutation, so
    # it fails fast like the GitHub reply. A near-limit slow-down after Bitbucket
    # already created the comment would delay persisting its id, and a cancel or
    # restart in that window posts a duplicate; a 429 is not retried either.
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_PR}/comments", status=status, json={"id": 503}, headers=headers)
    sleep = RecordingSleep()
    client = make_client(fake, sleep=sleep, near_limit_delay_seconds=2.5)
    if status == 200:
        assert await client.reply_to_thread(thread_id="bb:workspace/repo#42:100", body="d") == "503"
    else:
        with pytest.raises(BitbucketClientError) as excinfo:
            await client.reply_to_thread(thread_id="bb:workspace/repo#42:100", body="d")
        assert excinfo.value.status == 429
    assert sleep.delays == []
    assert len(fake.calls("POST")) == 1


async def test_reply_rejects_unrecognized_id() -> None:
    fake = FakeBitbucket()
    client = make_client(fake)
    with pytest.raises(BitbucketClientError):
        await client.reply_to_thread(thread_id="not-a-bb-thread", body="x")
    assert fake.requests == []


async def test_reply_raises_when_bitbucket_returns_no_comment_id() -> None:
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_PR}/comments", json={"type": "pullrequest_comment"})
    client = make_client(fake)
    with pytest.raises(BitbucketClientError) as excinfo:
        await client.reply_to_thread(thread_id="bb:workspace/repo#42:100", body="d")
    assert "no comment id" in str(excinfo.value)


@pytest.mark.parametrize("thread_id", [None, "PRRT_target"])
async def test_review_thread_audit_is_reported_unsupported(thread_id: str | None) -> None:
    fake = FakeBitbucket()
    client = make_client(fake)
    audit = await client.fetch_review_thread_audit(repo=repo(), pr_number=42, thread_id=thread_id)
    assert audit.supported is False
    assert audit.viewer_login is None
    assert audit.threads == ()
    assert fake.requests == []
