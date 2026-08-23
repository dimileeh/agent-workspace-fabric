"""Exact provider-neutral contract tests for PR-monitor agent verdicts."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.comment_verdict import (
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _final_non_empty_line,
    _parse_verdict_result,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            "analysis first\nAWF-VERDICT: FIXED: committed the null guard",
            VerdictResult(verdict="fix_committed", reason="committed the null guard"),
        ),
        (
            "AWF-VERDICT: FALSE POSITIVE: existing behavior is correct",
            VerdictResult(verdict="false_positive", reason="existing behavior is correct"),
        ),
        (
            "AWF-VERDICT: DEFER: track this in a follow-up",
            VerdictResult(verdict="defer", reason="track this in a follow-up"),
        ),
        (
            "AWF-VERDICT: NEEDS_HUMAN: choose the compatibility policy",
            VerdictResult(verdict="needs_human", reason="choose the compatibility policy"),
        ),
        (
            "AWF-VERDICT: FIXED: corrected the <summary> element handling",
            VerdictResult(
                verdict="fix_committed",
                reason="corrected the <summary> element handling",
            ),
        ),
        (
            "AWF-VERDICT: FALSE POSITIVE: **formatting is opaque**",
            VerdictResult(verdict="false_positive", reason="**formatting is opaque**"),
        ),
        (
            "AWF-VERDICT: DEFER: [<what to track>] is literal reason text",
            VerdictResult(verdict="defer", reason="[<what to track>] is literal reason text"),
        ),
        (
            "AWF-VERDICT: NEEDS_HUMAN: punctuation: []{}<>! is allowed",
            VerdictResult(
                verdict="needs_human",
                reason="punctuation: []{}<>! is allowed",
            ),
        ),
    ],
)
def test_exact_terminal_record_contract(stdout: str, expected: VerdictResult) -> None:
    assert _parse_verdict_result(stdout) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "ordinary final prose",
        "AWF-VERDICT: FIXED:",
        "AWF-VERDICT: FIXED:   ",
        "AWF-VERDICT: UNKNOWN: no such disposition",
        "awf-verdict: FIXED: wrong prefix case",
        "AWF-VERDICT: fixed: wrong label case",
        "AWF-VERDICT: NEEDS HUMAN: wrong separator",
        "AWF-VERDICT: NEEDS_HUMAN : wrong spacing",
        " AWF-VERDICT: FIXED: leading indentation",
        "\tAWF-VERDICT: FIXED: leading indentation",
        "**AWF-VERDICT: FIXED:** provider decoration",
        "**AWF-VERDICT: FIXED: provider decoration**",
        "- AWF-VERDICT: FIXED: list decoration",
        "> AWF-VERDICT: FIXED: quote decoration",
        "```\nAWF-VERDICT: FIXED: fenced example\n```",
        "<code>AWF-VERDICT: FIXED: html wrapper</code>",
        "AWF-VERDICT: FIXED: valid record\ntrailing non-empty output",
        ("AWF-VERDICT: DEFER: first record\nAWF-VERDICT: FALSE POSITIVE: second record"),
    ],
)
def test_any_non_contract_output_raises_typed_protocol_error(stdout: str) -> None:
    with pytest.raises(AgentVerdictProtocolError) as caught:
        _parse_verdict_result(stdout)

    assert caught.value.reason_code == "AGENT_VERDICT_PROTOCOL_VIOLATION"
    assert "ghp_" not in str(caught.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        "AWF-VERDICT: FALSE POSITIVE: <reason>",
        "AWF-VERDICT: FIXED: <one-sentence summary>",
        "AWF-VERDICT: NEEDS_HUMAN: <what you need>",
        "AWF-VERDICT: DEFER: <what to track>",
        "AWF-VERDICT: FALSE POSITIVE: &lt;reason&gt;",
        "AWF-VERDICT: FALSE POSITIVE: **<one-sentence justification>**",
        "AWF-VERDICT: DEFER: &lt;what to track&gt;",
        r"AWF-VERDICT: FALSE POSITIVE: \<one-sentence justification\>",
        "AWF-VERDICT: FALSE POSITIVE: [<one-sentence justification>]",
        "AWF-VERDICT: NEEDS_HUMAN: <what you need> and exit.",
        "AWF-VERDICT: DEFER: …",
        "AWF-VERDICT: FIXED: ...",
    ],
)
def test_template_placeholder_reason_raises_protocol_error(stdout: str) -> None:
    with pytest.raises(AgentVerdictProtocolError) as caught:
        _parse_verdict_result(stdout)

    assert caught.value.reason_code == "AGENT_VERDICT_PROTOCOL_VIOLATION"


@pytest.mark.unit
def test_reason_is_redacted_and_bounded_without_interpretation() -> None:
    secret = "ghp_exampletokenABCDEF0123456789"
    reason = secret + " " + ("x" * 600)

    result = _parse_verdict_result(f"AWF-VERDICT: NEEDS_HUMAN: {reason}")

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert secret not in result.reason
    assert len(result.reason) <= 500


@pytest.mark.unit
def test_redaction_only_reason_is_a_protocol_error() -> None:
    with pytest.raises(AgentVerdictProtocolError) as caught:
        _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN: ghp_exampletokenABCDEF0123456789")

    assert caught.value.reason_code == "AGENT_VERDICT_PROTOCOL_VIOLATION"


@pytest.mark.unit
def test_final_line_scan_does_not_split_or_copy_all_stdout() -> None:
    class NoSplitString(str):
        def splitlines(self, *args: object, **kwargs: object) -> list[str]:
            raise AssertionError("the terminal-line scan must not split all stdout")

    stdout = NoSplitString("x" * 100_000 + "\nAWF-VERDICT: DEFER: later\n\n")

    assert _final_non_empty_line(stdout) == "AWF-VERDICT: DEFER: later"
