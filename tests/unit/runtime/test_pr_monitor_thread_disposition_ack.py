"""A review bot's reply after AWF's disposition, judged once by the agent (#1138, D1).

Review bots often answer AWF's disposition reply ("Agreed, this is a false
positive"). The #1018 gate reads any comment after the marker as newer feedback,
so the marker goes stale and the #1138 fallback reopens the thread. When the
thread's own opener is a forge Bot account and every comment after AWF's newest
reply is that bot's, the gate first asks the agent once per comment identity
whether the reply only acknowledges the disposition. ACKNOWLEDGES keeps the
marker as the disposition (no reply, no reopen); anything else, and every human
or third-party comment, takes the fallback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_thread_audit import AuditedThreadComment
from awf.common.redaction import redact_secrets
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import (
    _disposition_ack_key,
    _disposition_ack_pending_key,
    _disposition_auto_reopen_key,
)
from awf.runtime.pr_monitor import NotifyHuman
from awf.runtime.pr_monitor_runner.thread_disposition import read_item_disposition
from awf.runtime.pr_monitor_runner.thread_disposition_ack import (
    ACKNOWLEDGES,
    MAX_ACK_REFETCH_FAILURES,
    NEW_POINT,
    UNJUDGED,
    ack_check_prompt,
    ack_reply_fits,
    acknowledgement_views,
    comment_identity,
    is_bot_concurrence,
    parse_ack_answer,
    record_ack_judgment,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._thread_disposition_fixtures import (
    BOT_ACK,
    acked_thread,
    thread_comment,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate import (
    _FRESH,
    _SILENT,
    _execute,
    _gate,
    _marker,
    _release_runner,
    _thread,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_reopen import (
    ReopenForge,
    audit_of,
    handled_state,
)

pytestmark = pytest.mark.unit

_PENDING = "REVIEW_THREAD_DISPOSITION_ACK_CHECK_PENDING"
_MARKER = _marker("false_positive", _SILENT)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def test_bot_ack_waits_for_one_check_then_certifies_without_reopen_or_reply(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    thread = acked_thread(_SILENT, _MARKER)
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(thread))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    with structlog.testing.capture_logs() as captured:
        terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    # Not certified yet, not reopened: the next poll's runner step judges the reply.
    assert terminal is False
    assert gh.unresolved == [] and gh.comments == [] and gh.thread_replies == []
    assert runner._deps.sleep.calls == [60]
    assert state.threads_addressed_ids[_disposition_ack_pending_key(_SILENT)] == "0"
    disposition = read_item_disposition(state, _SILENT)
    assert disposition is not None and disposition.reply_comment_id == "reply-1"  # not retired
    assert runner._deps.adapter.calls == []  # the gate never calls the agent
    logs = [
        r for r in captured if r["event"] == "monitor.review_thread_disposition_ack_check_pending"
    ]
    assert [(r["reason_code"], r["thread_ids"]) for r in logs] == [(_PENDING, [_SILENT])]

    record_ack_judgment(state, _SILENT, thread.trailing_comments[0], ACKNOWLEDGES)
    certified = await _gate(runner, workspace_id, state)

    assert certified.retry is False and certified.blocker is None
    assert certified.count_rows is not None
    assert dict(certified.count_rows)["false positive"] == 1
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids
    assert gh.unresolved == [] and gh.thread_replies == []
    disposition = read_item_disposition(state, _SILENT)
    assert disposition is not None and disposition.reply_comment_id == "reply-1"


@pytest.mark.parametrize("judgment", [NEW_POINT, UNJUDGED])
async def test_new_point_or_unjudged_record_takes_the_reopen_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    judgment: str,
) -> None:
    state = handled_state()
    thread = acked_thread(_SILENT, _MARKER)
    record_ack_judgment(state, _SILENT, thread.trailing_comments[0], judgment)
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(thread))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    assert terminal is False
    assert gh.unresolved == [_SILENT]
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "1"
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids
    assert runner._deps.adapter.calls == []


async def test_human_reply_after_marker_takes_the_fallback_without_ack_check(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    human = thread_comment("Agreed.", comment_id="3", login="octocat", is_bot=False)
    gh = ReopenForge(
        FakeCommandRunner(), audit=audit_of(acked_thread(_SILENT, _MARKER, trailing=(human,)))
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == [_SILENT]
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids
    assert runner._deps.adapter.calls == []


@pytest.mark.parametrize(
    ("opener", "reply"),
    [
        pytest.param(
            thread_comment("P2: x", comment_id="1"),
            thread_comment(BOT_ACK, comment_id="3", login="other-bot"),
            id="another-bot-replied",
        ),
        pytest.param(
            thread_comment("P2: x", comment_id="1", login="octocat", is_bot=False),
            thread_comment(BOT_ACK, comment_id="3"),
            id="a-human-opened-the-thread",
        ),
        pytest.param(
            thread_comment("P2: x", comment_id="1", login="octocat", is_bot=False),
            thread_comment(BOT_ACK, comment_id="3", login="octocat", is_bot=False),
            id="the-human-opener-replied",
        ),
    ],
)
async def test_bot_reply_by_a_bot_that_did_not_open_the_thread_takes_the_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    opener: AuditedThreadComment,
    reply: AuditedThreadComment,
) -> None:
    state = handled_state()
    thread = acked_thread(_SILENT, _MARKER, opener=opener, trailing=(reply,))
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(thread))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == [_SILENT]
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids


async def test_a_later_comment_by_anyone_else_takes_the_fallback_even_after_a_bot_ack(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    ack = thread_comment(BOT_ACK, comment_id="3")
    human = thread_comment("No, please fix it.", comment_id="4", login="octocat", is_bot=False)
    record_ack_judgment(state, _SILENT, ack, ACKNOWLEDGES)
    thread = acked_thread(_SILENT, _MARKER, trailing=(ack, human))
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(thread))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == [_SILENT]


async def test_edited_ack_comment_is_a_new_identity_and_is_checked_again(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    original = thread_comment(BOT_ACK, comment_id="3")
    edited = replace(original, body=f"{BOT_ACK} Actually, please also cover empty input.")
    record_ack_judgment(state, _SILENT, original, ACKNOWLEDGES)
    assert comment_identity(edited) != comment_identity(original)
    gh = ReopenForge(
        FakeCommandRunner(), audit=audit_of(acked_thread(_SILENT, _MARKER, trailing=(edited,)))
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    result = await _gate(runner, workspace_id, state)

    assert result.retry is True and result.feedback_changed is True
    assert result.blocker is None and result.count_rows is None
    assert state.threads_addressed_ids[_disposition_ack_pending_key(_SILENT)] == "0"
    assert _disposition_ack_key(_SILENT, comment_identity(edited)) not in (
        state.threads_addressed_ids
    )


async def test_unmarked_viewer_follow_up_is_not_ack_eligible(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # AWF's identity can be the operator's account: their unmarked follow-up is
    # newer feedback, so the bot's later reply cannot restore the old marker.
    state = handled_state()
    thread = acked_thread(
        _SILENT, _MARKER, viewer_bodies=(_MARKER, "Hmm, let me double-check this one.")
    )
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(thread))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == [_SILENT]
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids


async def test_edit_to_earlier_feedback_is_not_shielded_by_an_ack(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    thread = acked_thread(_SILENT, _MARKER, view_superseded=1)
    record_ack_judgment(state, _SILENT, thread.trailing_comments[0], ACKNOWLEDGES)
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(thread))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == [_SILENT]


async def test_a_pending_ack_check_does_not_hold_back_another_threads_reopen(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    unverified = _thread(_FRESH, external=("P1",))
    gh = ReopenForge(
        FakeCommandRunner(), audit=audit_of(acked_thread(_SILENT, _MARKER), unverified)
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == [_FRESH]
    assert state.threads_addressed_ids[_disposition_ack_pending_key(_SILENT)] == "0"


def test_comment_identity_changes_with_id_author_or_body() -> None:
    comment = thread_comment(BOT_ACK, comment_id="3")
    identity = comment_identity(comment)

    assert len(identity) == 64 and int(identity, 16) >= 0
    assert identity == comment_identity(thread_comment(BOT_ACK, comment_id="3"))
    variants = {
        identity,
        comment_identity(replace(comment, comment_id="4")),
        comment_identity(replace(comment, author_login="other-bot")),
        comment_identity(replace(comment, body=f"{BOT_ACK}!")),
    }
    assert len(variants) == 4


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("Reasoning.\nAWF-DISPOSITION-ACK: ACKNOWLEDGES\n", ACKNOWLEDGES),
        ("AWF-DISPOSITION-ACK: NEW_POINT", NEW_POINT),
        ("  AWF-DISPOSITION-ACK: NEW_POINT  \r\n\n", NEW_POINT),
        ("**AWF-DISPOSITION-ACK: ACKNOWLEDGES**", None),
        ("AWF-DISPOSITION-ACK: acknowledges", None),
        ("AWF-DISPOSITION-ACK: MAYBE", None),
        ("AWF-DISPOSITION-ACK: ACKNOWLEDGES\nAWF-DISPOSITION-ACK: ACKNOWLEDGES", ACKNOWLEDGES),
        ("AWF-DISPOSITION-ACK: NEW_POINT\nAWF-DISPOSITION-ACK: ACKNOWLEDGES", ACKNOWLEDGES),
        ("AWF-DISPOSITION-ACK: ACKNOWLEDGES\nanything after it", ACKNOWLEDGES),
        (
            "It agrees.\nAWF-DISPOSITION-ACK: ACKNOWLEDGES\n\nDone. Exiting now.\n",
            ACKNOWLEDGES,
        ),
        (
            "End with either\nAWF-DISPOSITION-ACK: ACKNOWLEDGES\nor\n"
            "AWF-DISPOSITION-ACK: NEW_POINT\nand print no other line containing "
            "AWF-DISPOSITION-ACK.\n**AWF-DISPOSITION-ACK: ACKNOWLEDGES**\n",
            NEW_POINT,
        ),
        ("I think it acknowledges.\nAWF-DISPOSITION-ACK: MAYBE\n", None),
        ("", None),
    ],
)
def test_parse_ack_answer_takes_the_last_exact_record(stdout: str, expected: str | None) -> None:
    assert parse_ack_answer(stdout) == expected


def test_ack_check_prompt_quotes_redacted_capped_evidence_and_forbids_changes() -> None:
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert redact_secrets(secret) != secret
    huge = "x" * 50_000

    prompt = ack_check_prompt("P2: guard the None payload", f"token {secret}", huge)

    assert "AWF-EVIDENCE> P2: guard the None payload" in prompt
    assert "AWF-EVIDENCE> token " in prompt and secret not in prompt
    assert huge not in prompt and len(prompt) < 20_000
    assert "do not modify files, run commands, or commit" in prompt
    assert "AWF-DISPOSITION-ACK: ACKNOWLEDGES" in prompt
    assert "AWF-DISPOSITION-ACK: NEW_POINT" in prompt


def test_only_a_reply_the_prompt_quotes_whole_fits_the_check() -> None:
    # A truncated reply could hide a new point past the cap, so it is never judged.
    assert ack_reply_fits("x" * 4000) is True
    assert ack_reply_fits("x" * 4001) is False


async def test_a_bot_reply_on_a_thread_resolved_by_another_login_is_not_checked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Someone else's resolution is their own disposition: nothing to verify or ask.
    state = handled_state()
    thread = acked_thread(_SILENT, _MARKER, resolved_by="octocat")
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(thread))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    result = await _gate(runner, workspace_id, state)

    assert result.count_rows is not None
    assert dict(result.count_rows)["resolved by others"] == 1
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids


def test_the_refetch_cap_fails_only_the_comments_still_unjudged() -> None:
    state = handled_state()
    ack = thread_comment(BOT_ACK, comment_id="3")
    later = thread_comment("Thanks again.", comment_id="4")
    record_ack_judgment(state, _SILENT, ack, ACKNOWLEDGES)
    state.mark_addressed(_disposition_ack_pending_key(_SILENT), str(MAX_ACK_REFETCH_FAILURES))
    thread = acked_thread(_SILENT, _MARKER, trailing=(ack, later))

    viewed, pending = acknowledgement_views(state, audit_of(thread), viewer="awf-operator")

    assert pending == frozenset()
    assert viewed.threads == (thread,)  # the full thread: the fallback re-triages it
    assert state.threads_addressed_ids[_disposition_ack_key(_SILENT, comment_identity(ack))] == (
        ACKNOWLEDGES
    )
    assert state.threads_addressed_ids[_disposition_ack_key(_SILENT, comment_identity(later))] == (
        UNJUDGED
    )
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids


# --- #1238: the deterministic concurrence check -------------------------------

_ANALYSIS_CHAIN = (
    "<details>\n<summary>🧩 Analysis chain</summary>\n\n"
    "🏁 Script executed:\n```shell\nrg -n 'payload is None' src/app.py\n```\n"
    "Output: src/app.py:12: if payload is None: should we raise?\n</details>"
)
_CODERABBIT_FOOTER = "<!-- This is an auto-generated reply by CodeRabbit -->"
# The three replies from the issue: each concurs with AWF's disposition.
CONCURRENCES = (
    "Agreed. The finding remains withdrawn.",
    "The current code has the guard, and the regression test covers this case... "
    "this is a fix rather than a false positive.",
    "I verified that commit 4827270 is in the current revision... I\u2019m leaving "
    "resolution to the AWF monitor.",
)
# One of them as the bot posts it: supporting material folded away, plus a footer.
CONCURRENCE_WITH_ANALYSIS = (
    f"@awf-operator Agreed. The finding remains withdrawn.\n\n{_ANALYSIS_CHAIN}\n\n"
    f"<details>\n<summary>✏️ Learnings added</summary>\n\n{'Learnt from: x. ' * 300}"
    f"\n</details>\n\n{_CODERABBIT_FOOTER}"
)
NEW_CONCERNS = (
    "Agreed, but the error path still lacks a test.",
    "Verified the guard. Could you also handle `None` in foo.py:42?",
    "Agreed. The finding remains withdrawn.\n\n<details>\n<summary>Proposed fix</summary>"
    "\n\n```suggestion\nif payload is None:\n    raise ValueError\n```\n</details>",
)


@pytest.mark.parametrize(
    "body",
    [
        *CONCURRENCES,
        CONCURRENCE_WITH_ANALYSIS,
        "LGTM, the fix is confirmed.",
        "Retracted.",
        "Verified the fix on the current head.",
        "The fix has been verified.",
    ],
)
def test_an_explicit_concurrence_is_recognised(body: str) -> None:
    assert is_bot_concurrence(body) is True


@pytest.mark.parametrize(
    "body",
    [
        *NEW_CONCERNS,
        "",
        "   \n ",
        BOT_ACK,  # thanks alone is no explicit concurrence: the agent judges it
        "The issue remains.",
        "Agreed that the bug persists.",
        "Agreed it is not fixed yet.",
        "I disagree; the finding stands.",
        "Agreed.\n```python\nx = 1\n```",
        "Agreed.\n~~~\nx = 1\n~~~",
        "Agreed; see line 42.",
        "Agreed. Please add a test.",
        "Agreed. Also, consider the empty case.",
        # Every clause must concur or state a fact: a new request alongside is a new point.
        "Agreed. Add a regression test for empty input.",
        "Agreed, add a test.",
        "Agreed; rename the helper.",
        "Agreed. The empty case needs a test.",
        "Agreed. It would be good to cover the empty case.",
        # Unbalanced or unterminated markup could hide a request: fail closed.
        "Agreed. <details><summary>More</summary>Add a test for the empty case.",
        "Agreed. </details> Add a test.",
        "Agreed. <!-- the rest is hidden",
        # A verification that confirms the finding, not the fix, is no concurrence.
        "Verified: the null dereference reproduces on the current head.",
        "Confirmed: the race reproduces.",
        # A concurrence only in quoted text is not the bot's own.
        "> Verified: the guard exists.\n\nHmm.",
        # A proposed change, even folded into a details block or a diff, is a new point.
        "Confirmed.\n<details><summary>Diff</summary>\n\n```diff\n-a\n+b\n```\n</details>",
        # A fold not labelled as supporting material could hold a request: the agent judges.
        "Agreed. <details><summary>New concern</summary>Please handle the empty input.</details>",
        "Agreed. <details><summary>Nit</summary>Rename the helper.</details>",
        "Agreed. <details>Rename the helper.</details>",
        "Agreed.\n<details><summary>🧩 Analysis chain</summary>\n"
        "<details><summary>Nit</summary>Rename the helper.</details>\n</details>",
    ],
)
def test_anything_short_of_an_explicit_concurrence_is_not_recognised(body: str) -> None:
    assert is_bot_concurrence(body) is False
