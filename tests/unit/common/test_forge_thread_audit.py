"""Predicate for AWF disposition replies in review-thread bodies (#1272)."""

from __future__ import annotations

from awf.common.forge_thread_audit import THREAD_DISPOSITION_MARKER_RE, is_disposition_reply

_THREAD = "PRRT_test_a"
_OTHER = "PRRT_other"


def _marker(*, cls: str = "fixed", thread: str = _THREAD) -> str:
    return f"<!-- AWF:thread-disposition:v1 class={cls} thread={thread} -->"


def test_is_disposition_reply_true_for_valid_marker_naming_thread() -> None:
    body = f"{_marker()}\n**AWF disposition: fixed.**"
    assert is_disposition_reply(body, thread_id=_THREAD) is True


def test_is_disposition_reply_false_for_wrong_thread() -> None:
    assert is_disposition_reply(_marker(thread=_OTHER), thread_id=_THREAD) is False


def test_is_disposition_reply_false_for_unknown_class() -> None:
    assert is_disposition_reply(_marker(cls="bogus"), thread_id=_THREAD) is False


def test_is_disposition_reply_false_when_marker_missing() -> None:
    assert is_disposition_reply("please fix this", thread_id=_THREAD) is False
    assert is_disposition_reply("", thread_id=_THREAD) is False


def test_is_disposition_reply_false_when_body_text_stripped_html_comments() -> None:
    # GraphQL bodyText strips HTML comments; a stripped body must not match.
    stripped = "**AWF disposition: fixed.**"
    assert is_disposition_reply(stripped, thread_id=_THREAD) is False


def test_marker_regex_is_shared_singleton() -> None:
    # One compiled pattern lives in forge_thread_audit and is reused elsewhere.
    assert THREAD_DISPOSITION_MARKER_RE.pattern == (
        r"<!-- AWF:thread-disposition:v1 class=(?P<cls>[a-z_]+) thread=(?P<thread>\S+) -->"
    )
