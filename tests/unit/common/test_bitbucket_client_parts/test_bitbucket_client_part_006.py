"""Bitbucket reviewer-task gating tests (issue #445).

Bitbucket exposes reviewer *tasks* separately from comments; a PR with open tasks
but no comments must not reach ``Merge``. These cover the ``/tasks`` fetch +
pagination, task→inline-thread mapping (so the address-comments gate blocks merge
and routes to AddressComments), the task-discriminated thread-id codec, and the
auto-resolve ``PUT`` (including the 403 → stable human-blocker reason code).
"""

from __future__ import annotations

import json

import pytest

from awf.common.bitbucket_client import (
    BITBUCKET_TASK_RESOLVE_FORBIDDEN,
    BitbucketClientError,
)
from awf.common.bitbucket_client_parsing import (
    build_unresolved_task_threads,
    decode_task_id,
    encode_task_id,
    is_task_thread_id,
)
from awf.runtime.pr_monitor import Merge, MonitorConfig, MonitorState, decide

from ._helpers import FakeBitbucket, make_client, pr_payload, repo

pytestmark = pytest.mark.unit

_HEAD = "s" * 40
_REPO = "/2.0/repositories/workspace/repo"
_PR = f"{_REPO}/pullrequests/42"


def _task(
    task_id: int,
    body: str,
    *,
    state: str = "UNRESOLVED",
    account: str = "reviewer-acct",
) -> dict:
    return {
        "id": task_id,
        "state": state,
        "content": {"raw": body},
        "creator": {"account_id": account, "display_name": "Reviewer"},
        "created_on": f"2024-02-{task_id:02d}T00:00:00+00:00",
        "links": {"html": {"href": f"https://bitbucket.org/t/{task_id}"}},
    }


def _seed_fetch_status(
    fake: FakeBitbucket,
    *,
    comments: list[dict] | None = None,
    tasks: list[dict] | None = None,
    account_id: str = "viewer",
) -> None:
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=comments or [])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page("GET", f"{_PR}/tasks", values=tasks or [])
    fake.enqueue("GET", "/2.0/user", json={"account_id": account_id})


# ── task-id codec ─────────────────────────────────────────────────────────────


def test_task_id_round_trip() -> None:
    tid = encode_task_id(repo=repo(), pr_number=42, task_id=7)
    assert tid == "bbtask:workspace/repo#42:7"
    assert is_task_thread_id(tid) is True
    assert decode_task_id(tid) == ("workspace", "repo", 42, "7")


def test_task_id_discriminated_from_comment_thread_id() -> None:
    # A comment thread id (``bb:``) must not be mistaken for a task id.
    assert is_task_thread_id("bb:workspace/repo#42:100") is False
    with pytest.raises(ValueError, match="Not a Bitbucket task id"):
        decode_task_id("bb:workspace/repo#42:100")


# ── task → inline-thread mapping ───────────────────────────────────────────────


def test_unresolved_task_maps_to_inline_thread() -> None:
    threads = build_unresolved_task_threads(
        [_task(7, "please add a test")],
        repo=repo(),
        pr_number=42,
        account_id="viewer",
    )
    assert len(threads) == 1
    thread = threads[0]
    assert thread.thread_id == "bbtask:workspace/repo#42:7"
    assert thread.path is None and thread.line is None
    assert thread.body_excerpt == "please add a test"
    assert [c.body for c in thread.comments] == ["please add a test"]


def test_resolved_task_is_dropped() -> None:
    assert (
        build_unresolved_task_threads(
            [_task(7, "done", state="RESOLVED")],
            repo=repo(),
            pr_number=42,
            account_id="viewer",
        )
        == ()
    )


def test_viewer_authored_task_is_dropped() -> None:
    assert (
        build_unresolved_task_threads(
            [_task(7, "mine", account="viewer")],
            repo=repo(),
            pr_number=42,
            account_id="viewer",
        )
        == ()
    )


def test_viewer_authored_task_with_uuid_only_creator_is_dropped() -> None:
    # Bitbucket may return a creator carrying only ``uuid`` (no ``account_id``), and
    # the viewer identity itself falls back to ``uuid``. Mirroring the comment identity
    # logic, a self-authored task in that shape must still be dropped — otherwise AWF
    # addresses/re-anchors on its own task.
    task = {
        "id": 7,
        "state": "UNRESOLVED",
        "content": {"raw": "mine"},
        "creator": {"uuid": "{viewer-uuid}", "display_name": "AWF"},
    }
    assert (
        build_unresolved_task_threads(
            [task],
            repo=repo(),
            pr_number=42,
            account_id="{viewer-uuid}",
        )
        == ()
    )


def test_empty_and_idless_tasks_are_dropped() -> None:
    tasks = [
        {"id": 8, "state": "UNRESOLVED", "content": {"raw": "   "}},
        {"state": "UNRESOLVED", "content": {"raw": "no id"}},
    ]
    assert (
        build_unresolved_task_threads(tasks, repo=repo(), pr_number=42, account_id="viewer") == ()
    )


# ── fetch_pr_status integration ────────────────────────────────────────────────


async def test_open_task_without_comments_blocks_merge() -> None:
    fake = FakeBitbucket()
    _seed_fetch_status(fake, comments=[], tasks=[_task(7, "please add a test")])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    # The headline bug: open tasks but no comments must yield non-empty feedback.
    assert len(status.unresolved_inline_threads) == 1
    assert status.unresolved_inline_threads[0].thread_id == "bbtask:workspace/repo#42:7"
    # decide() must NOT merge — it routes the task to AddressComments instead.
    action = decide(status, MonitorState(), MonitorConfig())
    assert not isinstance(action, Merge)


async def test_tasks_paginate() -> None:
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page(
        "GET",
        f"{_PR}/tasks",
        values=[_task(1, "task one")],
        next_url=f"https://api.bitbucket.org{_PR}/tasks?page=2",
    )
    fake.page("GET", f"{_PR}/tasks", values=[_task(2, "task two")], params={"page": "2"})
    fake.enqueue("GET", "/2.0/user", json={"account_id": "viewer"})
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    ids = {t.thread_id for t in status.unresolved_inline_threads}
    assert ids == {"bbtask:workspace/repo#42:1", "bbtask:workspace/repo#42:2"}


async def test_resolved_task_does_not_block() -> None:
    fake = FakeBitbucket()
    _seed_fetch_status(fake, tasks=[_task(7, "done", state="RESOLVED")])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.unresolved_inline_threads == ()


# ── auto-resolve PUT ───────────────────────────────────────────────────────────


async def test_resolve_task_issues_put_with_resolved_state() -> None:
    fake = FakeBitbucket()
    fake.enqueue("PUT", f"{_PR}/tasks/7", json={"id": 7, "state": "RESOLVED"})
    client = make_client(fake)
    await client.resolve_thread(thread_id="bbtask:workspace/repo#42:7")
    call = fake.calls("PUT")[0]
    assert call.url.path == f"{_PR}/tasks/7"
    assert json.loads(call.content) == {"state": "RESOLVED"}


async def test_resolve_task_forbidden_maps_to_stable_reason_code() -> None:
    fake = FakeBitbucket()
    fake.enqueue("PUT", f"{_PR}/tasks/7", status=403, json={"error": {"message": "no scope"}})
    client = make_client(fake)
    with pytest.raises(BitbucketClientError) as excinfo:
        await client.resolve_thread(thread_id="bbtask:workspace/repo#42:7")
    assert excinfo.value.reason_code == BITBUCKET_TASK_RESOLVE_FORBIDDEN
    assert excinfo.value.status == 403


async def test_resolve_task_non_forbidden_failure_propagates_unchanged() -> None:
    # A non-403 task-resolve failure (e.g. 500) propagates with its original reason
    # code so the fix-cycle treats it like any other resolve fault (transient wait or
    # generic blocker) rather than the task-scope human escalation.
    fake = FakeBitbucket()
    fake.enqueue("PUT", f"{_PR}/tasks/7", status=500, json={"error": {"message": "boom"}})
    client = make_client(fake)
    with pytest.raises(BitbucketClientError) as excinfo:
        await client.resolve_thread(thread_id="bbtask:workspace/repo#42:7")
    assert excinfo.value.reason_code != BITBUCKET_TASK_RESOLVE_FORBIDDEN
    assert excinfo.value.status == 500
