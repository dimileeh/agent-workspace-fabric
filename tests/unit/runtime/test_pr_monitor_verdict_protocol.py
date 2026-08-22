"""Focused contract tests for the PR-monitor stdout compatibility record."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers_verdict import _parse_verdict_result


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            "analysis first\nAWF-VERDICT: FIXED: committed the null guard",
            VerdictResult(verdict="fix_committed", reason="committed the null guard"),
        ),
        (
            "**AWF-VERDICT: FALSE POSITIVE:** existing behavior is correct",
            VerdictResult(verdict="false_positive", reason="existing behavior is correct"),
        ),
        (
            "**AWF-VERDICT: DEFER:** track this in a follow-up",
            VerdictResult(verdict="defer", reason="track this in a follow-up"),
        ),
        (
            "**AWF-VERDICT: NEEDS_HUMAN:** choose the compatibility policy",
            VerdictResult(verdict="needs_human", reason="choose the compatibility policy"),
        ),
        (
            "**AWF-VERDICT: FIXED:** committed the null guard",
            VerdictResult(verdict="fix_committed", reason="committed the null guard"),
        ),
        (
            "**AWF-VERDICT: FALSE POSITIVE: existing behavior is correct**",
            VerdictResult(verdict="false_positive", reason="existing behavior is correct"),
        ),
        (
            "**AWF-VERDICT: FIXED: committed the null guard**",
            VerdictResult(verdict="fix_committed", reason="committed the null guard"),
        ),
        (
            "**AWF-VERDICT: FALSE POSITIVE:** behavior is **expected**",
            VerdictResult(verdict="false_positive", reason="behavior is **expected**"),
        ),
        (
            "**AWF-VERDICT: FALSE POSITIVE:** literal trailing stars are content**",
            VerdictResult(
                verdict="false_positive",
                reason="literal trailing stars are content**",
            ),
        ),
        (
            "AWF-VERDICT: FIXED: corrected the <summary> element handling",
            VerdictResult(
                verdict="fix_committed",
                reason="corrected the <summary> element handling",
            ),
        ),
        (
            r"AWF-VERDICT: FIXED: corrected the \<summary\> element handling",
            VerdictResult(
                verdict="fix_committed",
                reason=r"corrected the \<summary\> element handling",
            ),
        ),
        (
            "AWF-VERDICT: FIXED: corrected the [<summary>] element handling",
            VerdictResult(
                verdict="fix_committed",
                reason="corrected the [<summary>] element handling",
            ),
        ),
        (
            "AWF-VERDICT: needs_human:",
            VerdictResult(verdict="needs_human", reason=None),
        ),
    ],
)
def test_final_verdict_record_contract(stdout: str, expected: VerdictResult) -> None:
    assert _parse_verdict_result(stdout) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        "AWF-VERDICT: FALSE POSITIVE: correct\nexplanation after the record",
        "*AWF-VERDICT: FALSE POSITIVE:* correct",
        "    AWF-VERDICT: FALSE POSITIVE: indented example",
        "\tAWF-VERDICT: FALSE POSITIVE: indented example",
        "- AWF-VERDICT: FALSE POSITIVE: correct",
        "> AWF-VERDICT: FALSE POSITIVE: correct",
        "# AWF-VERDICT: FALSE POSITIVE: correct",
        "`AWF-VERDICT: FALSE POSITIVE: correct`",
        "```\nAWF-VERDICT: FALSE POSITIVE: example\n```",
        "<code>AWF-VERDICT: FALSE POSITIVE: example</code>",
        "**AWF-VERDICT: FALSE POSITIVE: ** correct",
    ],
)
def test_non_record_formatting_fails_closed(stdout: str) -> None:
    result = _parse_verdict_result(stdout)

    assert result.verdict == "needs_human"
    assert result.reason in {
        "garbled_verdict_marker",
        "unrecognized_or_markerless_verdict",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", "empty_verdict_output"),
        ("ordinary final prose", "unrecognized_or_markerless_verdict"),
        ("AWF-VERDICT: UNKNOWN: no such disposition", "garbled_verdict_marker"),
        ("AWF-VERDICT: FIXED: <one-sentence summary>", "fixed_placeholder_echo"),
        (
            "AWF-VERDICT: FALSE POSITIVE: **<one-sentence justification>**",
            "verdict_placeholder_echo",
        ),
        (
            "AWF-VERDICT: DEFER: &lt;what to track&gt;",
            "verdict_placeholder_echo",
        ),
        (
            "AWF-VERDICT: FALSE POSITIVE: &amp;lt;reason&amp;gt;",
            "verdict_placeholder_echo",
        ),
        (
            "AWF-VERDICT: FALSE POSITIVE: &amp;amp;amp;amp;lt;reason&amp;amp;amp;amp;gt;",
            "verdict_placeholder_echo",
        ),
        (
            r"AWF-VERDICT: FALSE POSITIVE: \<one-sentence justification\>",
            "verdict_placeholder_echo",
        ),
        (
            r"AWF-VERDICT: DEFER: **\<what to track\>**",
            "verdict_placeholder_echo",
        ),
        (
            "AWF-VERDICT: FALSE POSITIVE: [<one-sentence justification>]",
            "verdict_placeholder_echo",
        ),
        (
            "AWF-VERDICT: DEFER: [**<what to track>**]",
            "verdict_placeholder_echo",
        ),
        (
            "AWF-VERDICT: FALSE POSITIVE: **ghp_abcdefghijklmnopqrstuvwxyz1234567890**",
            "verdict_placeholder_echo",
        ),
        (
            "AWF-VERDICT: FALSE POSITIVE: tentative AWF-VERDICT: NEEDS_HUMAN: choose policy",
            "garbled_verdict_marker",
        ),
        ("AWF-VERDICT: FALSE POSITIVE: <reason>", "verdict_placeholder_echo"),
        ("AWF-VERDICT: DEFER:", "verdict_placeholder_echo"),
    ],
)
def test_missing_or_invalid_record_fails_closed(stdout: str, reason: str) -> None:
    assert _parse_verdict_result(stdout) == VerdictResult(
        verdict="needs_human",
        reason=reason,
    )


@pytest.mark.unit
def test_reason_is_redacted_and_bounded() -> None:
    secret = "ghp_exampletokenABCDEF0123456789"
    reason = secret + " " + ("x" * 600)

    result = _parse_verdict_result(f"AWF-VERDICT: NEEDS_HUMAN: {reason}")

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert secret not in result.reason
    assert len(result.reason) <= 500
