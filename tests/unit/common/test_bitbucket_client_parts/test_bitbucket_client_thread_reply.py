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

from ._helpers import FakeBitbucket, make_client, repo

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


async def test_review_thread_audit_is_reported_unsupported() -> None:
    fake = FakeBitbucket()
    client = make_client(fake)
    audit = await client.fetch_review_thread_audit(repo=repo(), pr_number=42)
    assert audit.supported is False
    assert audit.viewer_login is None
    assert audit.threads == ()
    assert fake.requests == []
