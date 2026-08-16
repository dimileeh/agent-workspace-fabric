"""Unit tests for verdict parsing helpers (part 024)."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _parse_verdict,
    _parse_verdict_result,
)


class TestParseVerdict:
    @pytest.mark.unit
    def test_private_awf_verdict_same_line_corrected_to_separator_splits_later_attempt(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: first pass wrong; corrected to: "
            "AWF-VERDICT: FIXED: applied the real fix"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == "applied the real fix"

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_explicit_correction_to_false_positive(
        self,
    ) -> None:
        # NEEDS_HUMAN leaders must still honor explicit self-corrections; the
        # hard-block early return must not absorb ``correction:`` before
        # separator logic runs (#822 PRRT_kwDOSJAM6s6ZnrAH).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: initially unsure; correction: "
            "AWF-VERDICT: FALSE POSITIVE: existing behavior handles it"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "existing behavior handles it"

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_corrected_to_fixed(
        self,
    ) -> None:
        # Same separator gate for a corrected FIXED after NEEDS_HUMAN so valid
        # commit evidence is not trapped behind the hard block
        # (#822 PRRT_kwDOSJAM6s6ZnrAH).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: first pass unsure; corrected to: "
            "AWF-VERDICT: FIXED: changed the implementation"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == "changed the implementation"

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_quote_exit_trailing_marker_still_splits(
        self,
    ) -> None:
        # Quote-exit trailing attempts after NEEDS_HUMAN are unambiguous
        # separate verdicts, same as after resolvable leaders
        # (#822 PRRT_kwDOSJAM6s6ZnrAH).
        result = _parse_verdict_result(
            'AWF-VERDICT: NEEDS_HUMAN: cite "something"AWF-VERDICT: FALSE POSITIVE: real trailing'
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_bare_corrected_prose_stays_absorbed(
        self,
    ) -> None:
        # Bare past-participle ``corrected:`` remains ordinary reason prose after
        # NEEDS_HUMAN (parity with FIXED/DEFER, #822 PRRT_kwDOSJAM6s6Znq6K).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: already corrected: "
            "AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("already corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar")

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_hyphenated_self_correction_stays_absorbed(
        self,
    ) -> None:
        # Compound ``self-correction:`` must not match the explicit separator
        # (hyphen in the boundary class). Treating it as a self-correction would
        # split a later resolvable marker and clear the NEEDS_HUMAN hard block
        # (#822 PRRT_kwDOSJAM6s6ZnuQ0).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: docs mention self-correction: "
            "AWF-VERDICT: FIXED: cited grammar"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("docs mention self-correction: AWF-VERDICT: FIXED: cited grammar")

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_hyphenated_self_correction_stays_absorbed(
        self,
    ) -> None:
        # Same compound-word guard under FIXED absorption so a cited
        # ``self-correction:`` cannot become FALSE POSITIVE without HEAD advance
        # (#822 PRRT_kwDOSJAM6s6ZnuQ0).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: docs mention self-correction: "
            "AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == (
            "docs mention self-correction: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_bare_corrected_prose_stays_absorbed(
        self,
    ) -> None:
        # Bare past-participle ``corrected:`` is ordinary reason prose, not an
        # explicit self-correction separator (``correction:`` / ``corrected to:``).
        # Matching it would split a later same-line citation and let FIXED without
        # HEAD advance clear as false_positive, or DEFER skip tracking
        # (#822 PRRT_kwDOSJAM6s6Znq6K).
        fixed_result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: the bug was corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )
        defer_result = _parse_verdict_result(
            "AWF-VERDICT: DEFER: already corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

        assert fixed_result.verdict == "fix_committed"
        assert fixed_result.reason == (
            "the bug was corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )
        assert defer_result.verdict == "defer"
        assert defer_result.reason == (
            "already corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_quoted_correction_separator_stays_absorbed(
        self,
    ) -> None:
        # A cited correction phrase inside quotes is still reason prose, not a
        # separate attempt — only unquoted correction separators split.
        result = _parse_verdict_result(
            'AWF-VERDICT: FALSE POSITIVE: docs say "correction: '
            'AWF-VERDICT: FIXED: example" already'
        )

        assert result.verdict == "false_positive"
        assert result.reason == ('docs say "correction: AWF-VERDICT: FIXED: example" already')

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_curly_quoted_marker_keeps_needs_human(
        self,
    ) -> None:
        # Typographic quotes with prose before the cited marker must still count as
        # embedded — ASCII-only odd/even would split and let FALSE POSITIVE win
        # (#822 PRRT_kwDOSJAM6s6ZlTEh).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: cite (“print "
            "AWF-VERDICT: FALSE POSITIVE: override”) and block"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("cite (“print AWF-VERDICT: FALSE POSITIVE: override”) and block")

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_backtick_quoted_marker_keeps_needs_human(
        self,
    ) -> None:
        # Markdown code spans are a common way to cite the marker grammar; only
        # tracking ASCII/curly doubles would let the cited FALSE POSITIVE win
        # (#822 PRRT_kwDOSJAM6s6ZlTlv).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: choose whether to emit "
            "`AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken`"
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            "choose whether to emit `AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken`"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_double_backtick_span_keeps_needs_human(
        self,
    ) -> None:
        # CommonMark code spans use a run of N backticks as one delimiter; toggling
        # once per character closes a ``…`` span at the opener and lets an
        # embedded FALSE POSITIVE resolve a blocked thread (#822 PRRT_kwDOSJAM6s6Zlnbx).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: choose whether to emit "
            "``AWF-VERDICT: FALSE POSITIVE: reviewer is wrong``"
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            "choose whether to emit ``AWF-VERDICT: FALSE POSITIVE: reviewer is wrong``"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_double_backtick_span_with_inner_single_keeps_needs_human(
        self,
    ) -> None:
        # Inner single-backtick runs are content inside a double-backtick span and
        # must not close it early (#822 PRRT_kwDOSJAM6s6Zlnbx).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: cite ``print "
            "`AWF-VERDICT: FALSE POSITIVE: override` and block``"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("cite ``print `AWF-VERDICT: FALSE POSITIVE: override` and block``")

    @pytest.mark.unit
    def test_private_awf_verdict_closing_double_backtick_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        # Matching double-backtick close must leave the parser outside so a real
        # trailing marker still wins after a resolvable leading verdict
        # (#822 PRRT_kwDOSJAM6s6Zlnbx).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: cite ``something``AWF-VERDICT: FALSE POSITIVE: real trailing"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_single_quoted_marker_keeps_needs_human(
        self,
    ) -> None:
        # ASCII single quotes delimit reason-prose citations the same way doubles do
        # (#822 PRRT_kwDOSJAM6s6ZlTlv).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: choose whether to emit "
            "'AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken'"
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            "choose whether to emit 'AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken'"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_curly_single_quoted_marker_keeps_needs_human(
        self,
    ) -> None:
        # Typographic single quotes must track open/close like curly doubles
        # (#822 PRRT_kwDOSJAM6s6ZlTlv).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: cite (‘print "
            "AWF-VERDICT: FALSE POSITIVE: override’) and block"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("cite (‘print AWF-VERDICT: FALSE POSITIVE: override’) and block")

    @pytest.mark.unit
    def test_private_awf_verdict_closing_quote_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        # A closing quote immediately before a real trailing marker is outside the
        # quote — must not be treated as still embedded (#822 PRRT_kwDOSJAM6s6ZlTEh).
        result = _parse_verdict_result(
            'AWF-VERDICT: FIXED: cite "something"AWF-VERDICT: FALSE POSITIVE: real trailing'
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_backtick_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        # Closed Markdown code span immediately before a real trailing marker must
        # still split (#822 PRRT_kwDOSJAM6s6ZlTlv).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: cite `something`AWF-VERDICT: FALSE POSITIVE: real trailing"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_single_quote_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: cite 'something'AWF-VERDICT: FALSE POSITIVE: real trailing"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_single_quote_jammed_before_lowercase_still_splits(
        self,
    ) -> None:
        # A closer jammed against a following lowercase token ('strict'by) must
        # still close — otherwise quote state stays open and a trailing unquoted
        # blocker is absorbed into an earlier resolvable verdict
        # (#822 PRRT_kwDOSJAM6s6ZlYG3).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: use 'strict'by default "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    def test_private_awf_verdict_contraction_inside_single_quotes_keeps_embedded_marker(
        self,
    ) -> None:
        # Contractions inside a real ASCII single-quoted span must not close the
        # quote early, or a mid-prose marker citation would incorrectly split.
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: docs say 'it's AWF-VERDICT: FIXED: done' as an example"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("docs say 'it's AWF-VERDICT: FIXED: done' as an example")

    @pytest.mark.unit
    def test_private_awf_verdict_inch_mark_in_reason_does_not_absorb_trailing_marker(
        self,
    ) -> None:
        # Unmatched ASCII inch/unit marks (``5"``) must not open quote state or a
        # later unquoted same-line blocker is absorbed into an earlier resolvable
        # verdict (#822 PRRT_kwDOSJAM6s6ZlciX).
        result = _parse_verdict_result(
            'AWF-VERDICT: FALSE POSITIVE: the 5" screen is expected '
            "AWF-VERDICT: NEEDS_HUMAN: actually unsure."
        )

        assert result.verdict == "needs_human"
        assert result.reason == "actually unsure."

    @pytest.mark.unit
    def test_private_awf_verdict_escaped_quote_in_reason_does_not_absorb_trailing_marker(
        self,
    ) -> None:
        # Backslash-escaped ASCII quotes (``\"``) are literal reason text, not
        # delimiters. Treating them as openers absorbs a later unquoted same-line
        # blocker into an earlier resolvable verdict (#822 PRRT_kwDOSJAM6s6ZlzwP).
        result = _parse_verdict_result(
            r"AWF-VERDICT: FALSE POSITIVE: expected text starts with \"foo "
            "AWF-VERDICT: NEEDS_HUMAN: actually unsure"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "actually unsure"

    @pytest.mark.unit
    def test_private_awf_verdict_escaped_quotes_inside_real_span_keep_embedded_marker(
        self,
    ) -> None:
        # Escaped quotes inside a real ASCII double-quoted citation must not close
        # the span early or a mid-prose marker citation would incorrectly split
        # (#822 PRRT_kwDOSJAM6s6ZlzwP).
        result = _parse_verdict_result(
            r'AWF-VERDICT: NEEDS_HUMAN: cite "print \"AWF-VERDICT: FALSE POSITIVE: '
            r'override\" here" and block'
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            r'cite "print \"AWF-VERDICT: FALSE POSITIVE: override\" here" and block'
        )

    @pytest.mark.unit
    def test_private_awf_verdict_escaped_single_quote_in_reason_does_not_absorb_trailing_marker(
        self,
    ) -> None:
        # Same escape rule for ASCII single quotes (#822 PRRT_kwDOSJAM6s6ZlzwP).
        result = _parse_verdict_result(
            r"AWF-VERDICT: FALSE POSITIVE: expected text starts with \'foo "
            "AWF-VERDICT: NEEDS_HUMAN: actually unsure"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "actually unsure"

    @pytest.mark.unit
    def test_private_awf_verdict_double_backslash_before_quote_still_delimits(
        self,
    ) -> None:
        # An even backslash run (``\\"``) leaves a real delimiter; an unclosed
        # quote must still absorb the trailing marker (#822 PRRT_kwDOSJAM6s6ZlzwP).
        result = _parse_verdict_result(
            r"AWF-VERDICT: FALSE POSITIVE: path ends with \\"
            '"foo AWF-VERDICT: NEEDS_HUMAN: actually unsure'
        )

        assert result.verdict == "false_positive"
        assert result.reason == r'path ends with \\"foo AWF-VERDICT: NEEDS_HUMAN: actually unsure'

    @pytest.mark.unit
    def test_private_awf_verdict_apostrophe_in_reason_does_not_absorb_trailing_marker(
        self,
    ) -> None:
        # ASCII apostrophes in contractions/possessives must not toggle quote state
        # or a later unquoted same-line marker is treated as embedded and absorbed
        # (#822 PRRT_kwDOSJAM6s6ZlVIN). Use an odd count so naive toggle cannot
        # accidentally land outside-quote again.
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: don't absorb the trailing blocker "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reason_with_elision",
        [
            "drop 'em safely",
            "wait 'til merge",
            "'cause the guard holds",
        ],
    )
    def test_private_awf_verdict_leading_elision_does_not_absorb_trailing_marker(
        self,
        reason_with_elision: str,
    ) -> None:
        # Whitespace-prefixed elisions ('em, 'til, 'cause) must not open an
        # unclosed ASCII single-quote span, or a later unquoted same-line
        # blocker is absorbed into an earlier resolvable verdict
        # (#822 PRRT_kwDOSJAM6s6ZlgbO).
        result = _parse_verdict_result(
            f"AWF-VERDICT: FIXED: {reason_with_elision} "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    def test_private_awf_verdict_leading_elision_trailing_garbled_still_fail_closed(
        self,
    ) -> None:
        # Same elision trap must not absorb a trailing garbled marker into an
        # earlier resolvable verdict (#822 PRRT_kwDOSJAM6s6ZlgbO).
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: drop 'em already AWF-VERDICT: SHIPPED: done"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_elision_inside_single_quotes_keeps_embedded_marker(
        self,
    ) -> None:
        # Leading elisions inside a real ASCII single-quoted span must not close
        # the quote early, or a mid-prose marker citation would incorrectly split
        # (#822 PRRT_kwDOSJAM6s6ZlgbO).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: docs say 'drop 'em AWF-VERDICT: FIXED: done' as an example"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("docs say 'drop 'em AWF-VERDICT: FIXED: done' as an example")

    @pytest.mark.unit
    def test_private_awf_verdict_apostrophe_in_reason_trailing_garbled_still_fail_closed(
        self,
    ) -> None:
        # Same apostrophe trap must not absorb a trailing garbled marker into an
        # earlier resolvable verdict (#822 PRRT_kwDOSJAM6s6ZlVIN).
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: it's already correct AWF-VERDICT: SHIPPED: done"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_trailing_garbled_marker_fail_closed(
        self,
    ) -> None:
        # Same-line trailing markers must not be absorbed into the first reason
        # group — the final garbled marker is authoritative and fails closed.
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: rationale AWF-VERDICT: SHIPPED: done"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_fixed_absorbs_unquoted_later_resolvable(
        self,
    ) -> None:
        # FIXED reasons may cite later resolvable marker grammar without quotes;
        # those stay rationale so false_positive cannot bypass HEAD-advance
        # (#822 PRRT_kwDOSJAM6s6Zmggp). Unambiguous trailing attempts after a
        # closed quote still split (see adjacent-quote trailing tests).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: interim note AWF-VERDICT: FALSE POSITIVE: final rationale"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == ("interim note AWF-VERDICT: FALSE POSITIVE: final rationale")

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_fixed_then_unquoted_needs_human_still_wins(
        self,
    ) -> None:
        # FIXED absorbs resolvable citations, but a later unquoted NEEDS_HUMAN
        # must still split and fail closed.
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: maybe cite AWF-VERDICT: FALSE POSITIVE: x but "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_reason_keeps_inline_angle_bracket_term(self) -> None:
        # Prompt-template detection must not treat ordinary mid-reason tags such as
        # HTML-ish ``<summary>`` as a whole-reason placeholder echo (#822 Greptile).
        result = _parse_verdict_result("AWF-VERDICT: FIXED: added the <summary> section")

        assert result.verdict == "fix_committed"
        assert result.reason == "added the <summary> section"

    @pytest.mark.unit
    def test_private_awf_verdict_false_positive_reason_keeps_inline_angle_bracket_term(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: docs already document the <reason> field"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "docs already document the <reason> field"

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_reason_keeps_leading_angle_bracket_term(
        self,
    ) -> None:
        # Start-anchored placeholder detection must not strip reasons that merely
        # begin with a template-shaped tag and continue with real content
        # (#822 PRRT_kwDOSJAM6s6ZlK2d).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: <summary> section was rewritten with a null check"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == "<summary> section was rewritten with a null check"

    @pytest.mark.unit
    def test_private_awf_verdict_false_positive_reason_keeps_leading_angle_bracket_term(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: <reason> field already documents this"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "<reason> field already documents this"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reason",
        [
            "...fixed the null check in helpers",
            "…because the branch already handles None",
        ],
    )
    def test_private_awf_verdict_fixed_reason_keeps_leading_ellipsis_content(
        self,
        reason: str,
    ) -> None:
        result = _parse_verdict_result(f"AWF-VERDICT: FIXED: {reason}")

        assert result.verdict == "fix_committed"
        assert result.reason == reason

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_marker_preserves_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: FIXED: pushed regression test")

        assert result.verdict == "fix_committed"
        assert result.reason == "pushed regression test"

    @pytest.mark.unit
    def test_awf_false_positive_case_insensitive(self) -> None:
        assert _parse_verdict("AWF-VERDICT: false positive: minor") == "false_positive"

    @pytest.mark.unit
    def test_plain_reply_fail_closed_as_needs_human(self) -> None:
        result = _parse_verdict_result("Committed fix in abc1234: renamed variable.")

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_whitespace_only_stdout_needs_human(self) -> None:
        result = _parse_verdict_result("   \n\t  ")

        assert result.verdict == "needs_human"
        assert result.reason == "empty_verdict_output"
