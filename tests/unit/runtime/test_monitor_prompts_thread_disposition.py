"""Prompt and cert text for review-thread dispositions (#1018)."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import address_thread_prompt, ready_to_merge_comment
from awf.runtime.pr_monitor import ReviewThread

pytestmark = pytest.mark.unit


def _prompt() -> str:
    thread = ReviewThread(thread_id="PRRT_test_a", path="widgets/core.py", line=3, body_excerpt="")
    return address_thread_prompt(pr_number=1, repo_slug="acme/widgets", thread=thread)


def test_false_positive_requires_a_one_line_reason_that_awf_publishes() -> None:
    prompt = _prompt()
    assert "one-line reason is required" in prompt
    assert "AWF posts it in this review thread" in prompt
    assert "Do not write any PR comment or thread reply yourself" in prompt
    assert "Do not write any PR comment for verdict bookkeeping." not in prompt
    # The placeholder stays one the unchanged verdict parser rejects as a template
    # echo; ``<one-line reason>`` would be accepted as a real reason.
    assert "`AWF-VERDICT: FALSE POSITIVE: <one-sentence justification>`" in prompt


def test_defer_explains_the_in_thread_reply_instead_of_a_pr_comment() -> None:
    prompt = _prompt()
    assert "replies in this thread with your reason and the issue link" in prompt
    assert "posts an explanatory comment" not in prompt


def test_fixed_explains_the_commit_citing_reply() -> None:
    assert "cites your fix commit in its reply in this thread" in _prompt()


def test_ready_to_merge_renders_disposition_counts_on_line_one() -> None:
    body = ready_to_merge_comment(
        pr_number=1,
        head_sha="a" * 40,
        disposition_count_rows=(
            ("fixed", 2),
            ("false positive", 1),
            ("resolved by others", 3),
        ),
    )
    assert (
        "1. Inline comments resolved — AWF dispositions: fixed 2, false positive 1; "
        "resolved by others 3.\n"
    ) in body
    assert "1. Inline comments resolved.\n" not in body
    assert "2. Outside-diff comments addressed." in body


def test_ready_to_merge_lists_the_legacy_baseline_apart_from_awf_dispositions() -> None:
    body = ready_to_merge_comment(
        pr_number=1,
        head_sha="a" * 40,
        disposition_count_rows=(
            ("fixed", 2),
            ("resolved by others", 3),
            ("legacy baseline (resolved before AWF dispositions)", 4),
        ),
    )
    assert (
        "1. Inline comments resolved — AWF dispositions: fixed 2; resolved by others 3; "
        "legacy baseline (resolved before AWF dispositions) 4.\n"
    ) in body


@pytest.mark.parametrize("rows", [None, ()])
def test_ready_to_merge_without_counts_is_unchanged(
    rows: tuple[tuple[str, int], ...] | None,
) -> None:
    baseline = ready_to_merge_comment(pr_number=1, head_sha="a" * 40)
    assert (
        ready_to_merge_comment(pr_number=1, head_sha="a" * 40, disposition_count_rows=rows)
        == baseline
    )
    assert "1. Inline comments resolved.\n" in baseline


def test_counts_never_render_on_a_blocker_notification() -> None:
    body = ready_to_merge_comment(
        pr_number=1,
        head_sha="a" * 40,
        blocker_reason="CI is red",
        disposition_count_rows=(("fixed", 2), ("resolved by others", 0)),
    )
    assert "AWF dispositions" not in body


@pytest.mark.parametrize(
    "prefix",
    ["REVIEW_THREAD_DISPOSITION_UNVERIFIED:", "REVIEW_THREAD_AUDIT_UNAVAILABLE:"],
)
def test_disposition_blockers_survive_truncation_up_to_500_chars(prefix: str) -> None:
    reason = f"{prefix} " + ", ".join(f"PRRT_test_{i:03d}" for i in range(25))
    assert 160 < len(reason) <= 500
    body = ready_to_merge_comment(pr_number=1, head_sha="a" * 40, blocker_reason=reason)
    assert "PRRT_test_024" in body
