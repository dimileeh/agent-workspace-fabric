"""Unit tests for verdict parsing helpers (part 022)."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _monitor_state_verdict,
    _parse_verdict,
    _parse_verdict_result,
)


class TestParseVerdict:
    @pytest.mark.unit
    def test_empty_stdout_includes_fail_closed_reason(self) -> None:
        result = _parse_verdict_result("")

        assert result.verdict == "needs_human"
        assert result.reason == "empty_verdict_output"

    @pytest.mark.unit
    def test_garbled_awf_verdict_marker_fail_closed(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: COMPLETELY_BOGUS: not a real label")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_unrecognized_awf_verdict_label_fail_closed(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: SHIPPED: done")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_bare_only_mixed_verdicts_fail_closed(self) -> None:
        # Without an AWF marker, precedence among bare lines is irrelevant —
        # all fail closed rather than selecting FALSE POSITIVE / DEFER.
        reply = "FALSE POSITIVE: not a real issue.\nDEFER: follow-up issue"
        result = _parse_verdict_result(reply)

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_later_defer_does_not_overwrite_awf_needs_human(self) -> None:
        # AWF ``NEEDS_HUMAN`` must keep merge-blocking priority over later bare defer.
        reply = "AWF-VERDICT: NEEDS_HUMAN: follow-up needed\nDEFER: follow-up issue"
        assert _parse_verdict(reply) == "needs_human"

    @pytest.mark.unit
    def test_later_bare_false_positive_does_not_overwrite_awf_needs_human(self) -> None:
        # Bare false-positive text must not clear an AWF hard block.
        reply = "AWF-VERDICT: NEEDS_HUMAN: follow-up needed\nFALSE POSITIVE: not a real issue"
        assert _parse_verdict(reply) == "needs_human"

    @pytest.mark.unit
    def test_awf_false_positive_takes_precedence_over_awf_defer(self) -> None:
        reply = (
            "AWF-VERDICT: DEFER: fix this later\nAWF-VERDICT: FALSE POSITIVE: not a real problem"
        )
        assert _parse_verdict(reply) == "false_positive"

    @pytest.mark.unit
    def test_monitor_state_verdict_normalizes_persisted_private_verdicts(self) -> None:
        # #305: needs_human is now its own verdict, no longer collapsed to defer.
        assert _monitor_state_verdict("NEEDS_HUMAN") == "needs_human"
        assert _monitor_state_verdict("defer") == "defer"
        assert _monitor_state_verdict("agent_failed") == "agent_failed"
        assert _monitor_state_verdict("fixed") == "fix_committed"

    @pytest.mark.unit
    def test_many_same_line_quoted_citations_keep_leading_false_positive(self) -> None:
        # Many same-line quoted marker citations must absorb into the leading
        # resolvable verdict via one forward delimiter pass — rescanning each
        # prefix is quadratic in attacker-/agent-controlled output
        # (PRRT_kwDOSJAM6s6ZpHXP).
        citation = ' also cite "AWF-VERDICT: FIXED: example"'
        stdout = "AWF-VERDICT: FALSE POSITIVE: base rationale" + (citation * 2500)
        result = _parse_verdict_result(stdout)

        assert result.verdict == "false_positive"
        assert result.reason is not None
        assert result.reason.startswith("base rationale")

    @pytest.mark.unit
    def test_many_same_line_unquoted_citations_keep_leading_false_positive(self) -> None:
        # Unquoted mid-reason citations still absorb under FALSE POSITIVE; the
        # linear delimiter cursor must preserve that gate at scale.
        citation = " AWF-VERDICT: FIXED: cited grammar"
        stdout = "AWF-VERDICT: FALSE POSITIVE: base rationale" + (citation * 2500)
        result = _parse_verdict_result(stdout)

        assert result.verdict == "false_positive"
        assert result.reason is not None
        assert result.reason.startswith("base rationale")
