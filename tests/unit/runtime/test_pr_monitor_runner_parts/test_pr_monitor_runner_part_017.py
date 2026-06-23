"""Unit tests for verdict parsing helpers."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _monitor_state_verdict,
    _parse_verdict,
    _parse_verdict_result,
)


class TestParseVerdict:
    @pytest.mark.unit
    def test_empty_stdout_needs_human(self) -> None:
        # #305: empty agent output is a failure to produce, not a considered
        # defer. Block the merge (needs_human) rather than auto-capturing a
        # follow-up tracking issue on a thread the agent never addressed.
        assert _parse_verdict("") == "needs_human"

    @pytest.mark.unit
    def test_false_positive_marker(self) -> None:
        assert _parse_verdict("FALSE POSITIVE: reviewer misread the diff") == "false_positive"

    @pytest.mark.unit
    def test_private_awf_verdict_false_positive_marker(self) -> None:
        assert (
            _parse_verdict("AWF-VERDICT: FALSE POSITIVE: stale review boilerplate")
            == "false_positive"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_marker_preserves_reason(self) -> None:
        # #305: NEEDS_HUMAN maps to its own needs_human verdict (blocks merge,
        # never auto-resolved), distinct from a follow-up defer.
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    def test_private_awf_verdict_uses_final_line_not_prompt_echo(self) -> None:
        stdout = (
            'Re-reading: "print AWF-VERDICT: NEEDS_HUMAN: <what you need> and exit."\n'
            "Some deliberation about the tradeoff.\n"
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose the checkout policy"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must choose the checkout policy"

    @pytest.mark.unit
    def test_private_awf_mixed_verdict_prefers_awf_over_bare_fallback(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: maintainer decision required\nFALSE POSITIVE: maintainer later added a comment"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision required"

    @pytest.mark.unit
    def test_private_awf_later_verdict_wins_over_prior_verdict(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: stale review boilerplate\n"
            "AWF-VERDICT: NEEDS_HUMAN: maintainer follow-up required"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer follow-up required"

    @pytest.mark.unit
    def test_private_awf_multiple_needs_human_uses_latest_reason(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: first pass needs human review\nAWF-VERDICT: NEEDS_HUMAN:"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "first pass needs human review"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_inline_prompt_template(self) -> None:
        stdout = (
            'Re-reading: "If you need a human decision, print '
            'AWF-VERDICT: NEEDS_HUMAN: <what you need> and exit."'
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_placeholder_only_needs_human_has_no_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN: <what you need>")

        assert result.verdict == "needs_human"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_without_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN:")

        assert result.verdict == "needs_human"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_defer_without_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: DEFER:")

        assert result.verdict == "defer"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_space_variant_preserves_reason(self) -> None:
        # The primary _AWF_VERDICT regex tolerates "NEEDS HUMAN" (space) like
        # "FALSE POSITIVE", so the reason is extracted cleanly instead of being
        # garbled by the bare fallback (which splits on the AWF-VERDICT colon).
        result = _parse_verdict_result("AWF-VERDICT: NEEDS HUMAN: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "label",
        [
            "NEEDS_HUMAN",
            "NEEDS HUMAN",
            "NEEDS_ HUMAN",
            "NEEDS _HUMAN",
            "NEEDS__HUMAN",
            "needs_human",
        ],
    )
    def test_private_awf_verdict_needs_human_separator_variants(self, label: str) -> None:
        # Any separator the NEEDS[\s_]+HUMAN regex accepts must normalize to
        # needs_human — never silently fall through to fix_committed (#305).
        result = _parse_verdict_result(f"AWF-VERDICT: {label}: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    def test_private_awf_verdict_defer_placeholder_only_has_no_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: DEFER: <defer follow-up needed>")

        assert result.verdict == "defer"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_marker_preserves_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: FIXED: pushed regression test")

        assert result.verdict == "fix_committed"
        assert result.reason == "pushed regression test"

    @pytest.mark.unit
    def test_false_positive_case_insensitive(self) -> None:
        assert _parse_verdict("false positive: minor") == "false_positive"

    @pytest.mark.unit
    def test_defer_marker(self) -> None:
        assert _parse_verdict("DEFER: needs human judgement") == "defer"

    @pytest.mark.unit
    def test_plain_reply_counts_as_fix_committed(self) -> None:
        assert _parse_verdict("Committed fix in abc1234: renamed variable.") == "fix_committed"

    @pytest.mark.unit
    def test_later_defer_does_not_overwrite_prior_false_positive_marker(self) -> None:
        # Hardening keeps blocking verdicts from being demoted by a later defer.
        reply = "FALSE POSITIVE: not a real issue.\nDEFER: follow-up issue"
        assert _parse_verdict(reply) == "false_positive"

    @pytest.mark.unit
    def test_later_defer_does_not_overwrite_bare_needs_human(self) -> None:
        # ``NEEDS_HUMAN`` must keep merge-blocking priority over later defer text.
        reply = "NEEDS_HUMAN: follow-up needed\nDEFER: follow-up issue"
        assert _parse_verdict(reply) == "needs_human"

    @pytest.mark.unit
    def test_later_false_positive_does_not_overwrite_bare_needs_human(self) -> None:
        # ``NEEDS_HUMAN`` must keep merge-blocking priority over later false-positive
        # text so a non-blocking later marker cannot clear a hard block.
        reply = "NEEDS_HUMAN: follow-up needed\nFALSE POSITIVE: not a real issue"
        assert _parse_verdict(reply) == "needs_human"

    @pytest.mark.unit
    def test_bare_false_positive_takes_precedence_over_bare_defer(self) -> None:
        reply = "DEFER: fix this later\nFALSE POSITIVE: not a real problem"
        assert _parse_verdict(reply) == "false_positive"

    @pytest.mark.unit
    def test_monitor_state_verdict_normalizes_persisted_private_verdicts(self) -> None:
        # #305: needs_human is now its own verdict, no longer collapsed to defer.
        assert _monitor_state_verdict("NEEDS_HUMAN") == "needs_human"
        assert _monitor_state_verdict("defer") == "defer"
        assert _monitor_state_verdict("agent_failed") == "agent_failed"
        assert _monitor_state_verdict("fixed") == "fix_committed"
